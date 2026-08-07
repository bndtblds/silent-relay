#!/bin/sh
set -eu

umask 077
cd "$(dirname "$0")"

fail() {
    printf '%s\n' "Update failed: $*" >&2
    exit 2
}

[ "$#" -eq 0 ] || fail "usage: sh update.sh"

for command in git docker; do
    command -v "$command" >/dev/null 2>&1 || fail "$command is required"
done
[ -f .env ] || fail ".env does not exist"
[ -f .backup.conf ] || fail ".backup.conf does not exist; configure encrypted backups first"
[ -f .backup-transfer.conf ] || fail ".backup-transfer.conf does not exist; configure off-site backup transfer first"
[ -f backup.sh ] || fail "backup.sh does not exist"
[ -f backup-transfer.sh ] || fail "backup-transfer.sh does not exist"

worktree_status=$(git status --porcelain) || fail "cannot inspect the Git worktree"
[ -z "$worktree_status" ] || fail "the Git worktree is not clean; inspect git status before updating"

old_commit=$(git rev-parse HEAD) || fail "cannot determine the installed commit"

backup_output=$(sh backup.sh) || fail "the encrypted backup did not complete"
printf '%s\n' "$backup_output"
backup_file=$(printf '%s\n' "$backup_output" | sed -n 's/^Backup created: //p' | tail -n 1)
[ -n "$backup_file" ] || fail "backup.sh did not report a completed encrypted backup"
[ -f "$backup_file" ] && [ ! -L "$backup_file" ] || fail "the reported encrypted backup is not a regular file"
case "$backup_file" in *.tar.gz.age) ;; *) fail "the reported backup is not an encrypted SilentRelay archive";; esac

sh backup-transfer.sh "$backup_file" || fail "the off-site backup transfer did not complete"

git pull --ff-only || fail "Git could not fast-forward the installed branch"
new_commit=$(git rev-parse HEAD) || fail "cannot determine the updated commit"

if ! docker compose up -d --build --wait --wait-timeout 120; then
    printf '%s\n' "Migration or service startup failed. Inspect: docker compose logs migrate web scheduler caddy" >&2
    fail "the updated deployment is not ready; no automatic rollback was attempted"
fi

docker compose exec -T web python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)" \
    || fail "the readiness check failed; no automatic rollback was attempted"

printf '%s\n' "Update completed: $old_commit -> $new_commit"
if [ "$old_commit" = "$new_commit" ]; then
    printf '%s\n' "No new commits were available."
else
    git log --oneline "$old_commit..$new_commit"
fi
printf '%s\n' "The database may have been migrated. Rollback is never automatic; restore deliberately from the verified backup if recovery is required."
