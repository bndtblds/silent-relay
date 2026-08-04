#!/bin/sh
set -eu

umask 077
cd "$(dirname "$0")"

fail() {
    printf '%s\n' "Restore failed: $*" >&2
    exit 2
}

replace_existing=false
if [ "${1:-}" = "--replace" ]; then
    replace_existing=true
    shift
fi
[ "$#" -eq 2 ] || fail "usage: sh restore.sh [--replace] BACKUP_FILE PRIVATE_AGE_IDENTITY_FILE"
backup_file=$1
identity_file=$2
[ -f "$backup_file" ] || fail "backup file does not exist"
[ -f "$identity_file" ] || fail "private age identity file does not exist"
if [ -e .env ] && [ "$replace_existing" = false ]; then
    fail ".env already exists; use --replace only for an intentional recovery"
fi
command -v age >/dev/null 2>&1 || fail "age is required"
docker version >/dev/null
docker compose config --quiet
docker compose --profile maintenance build backup restore

if [ "$replace_existing" = true ]; then
    [ -e .env ] || fail "--replace requires an existing installation"
    printf '%s\n' "The current installation will first be backed up, then replaced."
    printf '%s' "Type REPLACE to continue: "
    expected_confirmation=REPLACE
else
    printf '%s\n' "This restores configuration and application data into this fresh installation."
    printf '%s' "Type RESTORE to continue: "
    expected_confirmation=RESTORE
fi
read -r confirmation
[ "$confirmation" = "$expected_confirmation" ] || fail "confirmation was not entered"

if [ "$replace_existing" = true ]; then
    printf '%s\n' "Creating the mandatory pre-restore safety backup."
    SILENTRELAY_PROTECTED_BACKUP=$(realpath "$backup_file") sh backup.sh
    docker compose stop web scheduler
fi

pipe_directory=$(mktemp -d "./.restore-pipe.XXXXXX")
fifo="$pipe_directory/archive"
mkfifo "$fifo"
cleanup() {
    rm -f "$fifo"
    rmdir "$pipe_directory" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

# Authenticate the complete archive and reject code older than the backup before
# writing configuration or data. Decryption is intentionally repeated for the
# actual restore so no plaintext archive is kept on disk.
age -d -i "$identity_file" -o - "$backup_file" > "$fifo" &
age_pid=$!
set +e
backup_commit=$(docker compose --profile maintenance run --rm --no-deps -T backup inspect \
    --field git_commit < "$fifo")
inspect_status=$?
wait "$age_pid"
age_status=$?
set -e
[ "$age_status" -eq 0 ] || fail "the backup could not be decrypted"
[ "$inspect_status" -eq 0 ] || fail "the backup failed validation"
git cat-file -e "$backup_commit^{commit}" 2>/dev/null || \
    fail "the backup commit is not available; fetch repository history first"
git merge-base --is-ancestor "$backup_commit" HEAD || \
    fail "the checked-out software is older than or incompatible with the backup"

age -d -i "$identity_file" -o - "$backup_file" > "$fifo" &
age_pid=$!
set +e
replace_argument=""
[ "$replace_existing" = false ] || replace_argument="--replace-existing"
manifest=$(docker compose --profile maintenance run --rm --no-deps -T restore restore \
    --data-dir /data --env-file /config/.env $replace_argument < "$fifo")
restore_status=$?
wait "$age_pid"
age_status=$?
set -e
[ "$age_status" -eq 0 ] || fail "the backup could not be decrypted"
[ "$restore_status" -eq 0 ] || fail "the backup failed validation or restoration"

docker compose run --rm migrate
docker compose up -d --wait --wait-timeout 120
docker compose ps
printf '%s\n' "Restore completed. Verify /health/ready, sign in, and run a test notification."
printf '%s\n' "Backup manifest: $manifest"
