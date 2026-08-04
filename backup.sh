#!/bin/sh
set -eu

umask 077
cd "$(dirname "$0")"

CONFIG_FILE=${SILENTRELAY_BACKUP_CONFIG:-.backup.conf}

fail() {
    printf '%s\n' "Backup failed: $*" >&2
    exit 2
}

read_config_value() {
    key=$1
    count=$(grep -c "^${key}=" "$CONFIG_FILE" || true)
    [ "$count" -eq 1 ] || fail "configuration must contain exactly one ${key} entry"
    sed -n "s/^${key}=//p" "$CONFIG_FILE"
}

configure_backup() {
    [ -t 0 ] || fail "$CONFIG_FILE is missing; run ./backup.sh interactively first"
    command -v age >/dev/null 2>&1 || fail "age is required; install age and run the command again"
    command -v age-keygen >/dev/null 2>&1 || fail "age-keygen is required"

    printf '%s\n' "SilentRelay backups contain the database and its matching encryption keys."
    printf '%s\n' "Only the private age identity file can decrypt and restore them."
    printf '%s\n' "Keep that private identity off this server after setup."
    printf '%s' "Use an existing public age recipient (age1...)? [y/N] "
    read -r existing
    case "$existing" in
        y|Y|yes|YES)
            printf '%s' "Public age recipient (age1...): "
            read -r recipient
            ;;
        *)
            default_identity="${XDG_CONFIG_HOME:-$HOME/.config}/silent-relay/backup-age-identity.key"
            printf 'Private age identity file [%s]: ' "$default_identity"
            read -r identity_path
            identity_path=${identity_path:-$default_identity}
            [ ! -e "$identity_path" ] || fail "$identity_path already exists"
            mkdir -p "$(dirname "$identity_path")"
            chmod 700 "$(dirname "$identity_path")"
            age-keygen -o "$identity_path"
            chmod 600 "$identity_path"
            recipient=$(age-keygen -y "$identity_path")
            probe=$(mktemp)
            trap 'rm -f "$probe"' EXIT HUP INT TERM
            printf '%s' "SilentRelay backup key test" | age -r "$recipient" -o "$probe"
            age -d -i "$identity_path" "$probe" >/dev/null
            rm -f "$probe"
            trap - EXIT HUP INT TERM
            printf '\n%s\n' "The key pair passed an encryption and decryption test."
            printf '%s\n' "Copy the private age identity file $identity_path to a separate offline location before relying on backups."
            ;;
    esac
    printf '%s' "Backup directory [/var/backups/silent-relay]: "
    read -r backup_directory
    backup_directory=${backup_directory:-/var/backups/silent-relay}
    installation_id=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)
    {
        printf 'BACKUP_DIRECTORY=%s\n' "$backup_directory"
        printf 'KEEP_BACKUPS=7\n'
        printf 'AGE_RECIPIENT=%s\n' "$recipient"
        printf 'INSTALLATION_ID=%s\n' "$installation_id"
    } > "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    printf '%s\n' "Created $CONFIG_FILE with retention of seven successful backups."
}

[ -f "$CONFIG_FILE" ] || configure_backup

unknown=$(grep -Ev '^(BACKUP_DIRECTORY|KEEP_BACKUPS|AGE_RECIPIENT|INSTALLATION_ID)=[^[:cntrl:]]+$|^[[:space:]]*(#|$)' "$CONFIG_FILE" || true)
[ -z "$unknown" ] || fail "$CONFIG_FILE contains unknown or invalid entries"

backup_directory=$(read_config_value BACKUP_DIRECTORY)
keep_backups=$(read_config_value KEEP_BACKUPS)
recipient=$(read_config_value AGE_RECIPIENT)
installation_id=$(read_config_value INSTALLATION_ID)
case "$keep_backups" in *[!0-9]*|'') fail "KEEP_BACKUPS must be a positive number";; esac
[ "$keep_backups" -ge 1 ] || fail "KEEP_BACKUPS must be at least 1"
case "$installation_id" in *[!A-Za-z0-9-]*|'') fail "INSTALLATION_ID is invalid";; esac
case "$recipient" in age1*) ;; *) fail "AGE_RECIPIENT must be a public age recipient beginning with age1";; esac

command -v age >/dev/null 2>&1 || fail "age is required"
docker version >/dev/null
docker compose config --quiet
[ -f .env ] || fail ".env does not exist"
# Build before opening the binary pipeline. Compose build progress must never be
# allowed to enter the archive stream when the maintenance image is missing.
docker compose --profile maintenance build backup
mkdir -p "$backup_directory"

lock_directory="$backup_directory/.silentrelay-backup.lock"
mkdir "$lock_directory" 2>/dev/null || fail "another backup or restore operation is active"
pipe_directory=$(mktemp -d "$backup_directory/.backup-pipe.XXXXXX")
fifo="$pipe_directory/archive"
mkfifo "$fifo"
running_services=$(docker compose ps --status running --services)
restart_services=""
for service in web scheduler; do
    if printf '%s\n' "$running_services" | grep -qx "$service"; then
        restart_services="$restart_services $service"
    fi
done

cleanup() {
    rm -f "$fifo" "${partial_file:-}"
    rmdir "$pipe_directory" 2>/dev/null || true
    rmdir "$lock_directory" 2>/dev/null || true
    if [ -n "$restart_services" ]; then
        docker compose up -d $restart_services >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT HUP INT TERM

[ -z "$restart_services" ] || docker compose stop $restart_services
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
commit=$(git rev-parse HEAD)
final_file="$backup_directory/silentrelay-${installation_id}-${timestamp}.tar.gz.age"
partial_file="$final_file.partial"

age -r "$recipient" -o "$partial_file" < "$fifo" &
age_pid=$!
set +e
docker compose --profile maintenance run --rm --no-deps -T backup create \
    --data-dir /data --env-file /config/.env --commit "$commit" \
    --installation-id "$installation_id" --age-recipient "$recipient" \
    --created-at "$created_at" > "$fifo"
producer_status=$?
wait "$age_pid"
age_status=$?
set -e
[ "$producer_status" -eq 0 ] || fail "the data archive could not be created"
[ "$age_status" -eq 0 ] || fail "the data archive could not be encrypted"
chmod 600 "$partial_file"
mv "$partial_file" "$final_file"
partial_file=""

# Remove only completed archives created for this installation, and only after success.
index=0
old_ifs=$IFS
IFS='
'
for old_backup in $(ls -1t "$backup_directory"/silentrelay-"$installation_id"-*.tar.gz.age 2>/dev/null || true); do
    index=$((index + 1))
    if [ "$index" -gt "$keep_backups" ]; then
        if [ -n "${SILENTRELAY_PROTECTED_BACKUP:-}" ] && \
            [ "$(realpath "$old_backup")" = "$(realpath "$SILENTRELAY_PROTECTED_BACKUP")" ]; then
            continue
        fi
        rm -f -- "$old_backup"
        printf '%s\n' "Removed expired backup: ${old_backup##*/}"
    fi
done
IFS=$old_ifs

printf '%s\n' "Backup created: $final_file"
printf '%s\n' "Keep a copy on a separate system and test restoration regularly."
