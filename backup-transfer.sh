#!/bin/sh
set -eu

umask 077
cd "$(dirname "$0")"

CONFIG_FILE=${SILENTRELAY_BACKUP_TRANSFER_CONFIG:-.backup-transfer.conf}
BACKUP_CONFIG=${SILENTRELAY_BACKUP_CONFIG:-.backup.conf}

fail() {
    printf '%s\n' "Backup transfer failed: $*" >&2
    exit 2
}

read_value() {
    file=$1
    key=$2
    count=$(grep -c "^${key}=" "$file" || true)
    [ "$count" -eq 1 ] || fail "$file must contain exactly one ${key} entry"
    sed -n "s/^${key}=//p" "$file"
}

require_private_file() {
    file=$1
    label=$2
    [ -f "$file" ] && [ ! -L "$file" ] || fail "$label must be a regular file"
    mode=$(stat -c '%a' "$file" 2>/dev/null) || fail "cannot inspect $label permissions"
    [ "$mode" = 600 ] || fail "$label must have mode 0600"
}

[ -f "$CONFIG_FILE" ] || fail "$CONFIG_FILE does not exist"
[ -f "$BACKUP_CONFIG" ] || fail "$BACKUP_CONFIG does not exist"

target=$(read_value "$CONFIG_FILE" TRANSFER_TARGET)
installation_id=$(read_value "$BACKUP_CONFIG" INSTALLATION_ID)
backup_directory=$(read_value "$BACKUP_CONFIG" BACKUP_DIRECTORY)
case "$installation_id" in *[!A-Za-z0-9-]*|'') fail "INSTALLATION_ID is invalid";; esac

if [ "$#" -gt 1 ]; then
    fail "usage: sh backup-transfer.sh [encrypted-backup]"
elif [ "$#" -eq 1 ]; then
    archive=$1
else
    archive=$(ls -1t "$backup_directory"/silentrelay-"$installation_id"-*.tar.gz.age 2>/dev/null | head -n 1 || true)
    [ -n "$archive" ] || fail "no completed backup exists for this installation"
fi

[ -f "$archive" ] && [ ! -L "$archive" ] || fail "the selected backup must be a regular file"
filename=${archive##*/}
case "$filename" in
    silentrelay-"$installation_id"-*.tar.gz.age) ;;
    *) fail "the selected file is not a completed backup for this installation";;
esac
case "$filename" in *[!A-Za-z0-9._-]*) fail "the backup filename is invalid";; esac
local_size=$(stat -c '%s' "$archive") || fail "cannot inspect the selected backup"
[ "$local_size" -gt 0 ] || fail "the selected backup is empty"
unique=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)
partial_name=".${filename}.${unique}.partial"

transfer_sftp() {
    unknown=$(grep -Ev '^(TRANSFER_TARGET|SFTP_HOST|SFTP_PORT|SFTP_USER|SFTP_DIRECTORY|SFTP_IDENTITY_FILE|SFTP_KNOWN_HOSTS_FILE)=[^[:cntrl:]]+$|^[[:space:]]*(#|$)' "$CONFIG_FILE" || true)
    [ -z "$unknown" ] || fail "$CONFIG_FILE contains unknown or invalid SFTP entries"
    host=$(read_value "$CONFIG_FILE" SFTP_HOST)
    port=$(read_value "$CONFIG_FILE" SFTP_PORT)
    user=$(read_value "$CONFIG_FILE" SFTP_USER)
    directory=$(read_value "$CONFIG_FILE" SFTP_DIRECTORY)
    identity=$(read_value "$CONFIG_FILE" SFTP_IDENTITY_FILE)
    known_hosts=$(read_value "$CONFIG_FILE" SFTP_KNOWN_HOSTS_FILE)
    case "$host" in *[!A-Za-z0-9.-]*|'') fail "SFTP host is invalid";; esac
    case "$user" in *[!A-Za-z0-9._-]*|'') fail "SFTP user is invalid";; esac
    case "$port" in *[!0-9]*|'') fail "SFTP_PORT is invalid";; esac
    [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || fail "SFTP_PORT is invalid"
    case "$directory" in /*) ;; *) fail "SFTP_DIRECTORY must be absolute";; esac
    case "$directory" in *..*|*[!A-Za-z0-9._/-]*) fail "SFTP_DIRECTORY is invalid";; esac
    require_private_file "$identity" "SFTP identity file"
    [ -f "$known_hosts" ] && [ ! -L "$known_hosts" ] || fail "SFTP known-hosts file must be a regular file"
    command -v sftp >/dev/null 2>&1 || fail "sftp is required"
    remote_final="$directory/$filename"
    remote_partial="$directory/$partial_name"
    batch=$(mktemp)
    listing=$(mktemp)
    cleanup_sftp() {
        if [ -n "${remote_partial:-}" ]; then
            printf 'rm %s\n' "$remote_partial" > "$batch"
            sftp -q -b "$batch" -i "$identity" -oBatchMode=yes \
                -oIdentitiesOnly=yes -oStrictHostKeyChecking=yes \
                -oUserKnownHostsFile="$known_hosts" -P "$port" "$user@$host" >/dev/null 2>&1 || true
        fi
        rm -f "$batch" "$listing"
    }
    trap cleanup_sftp EXIT HUP INT TERM
    printf 'ls %s\n' "$remote_final" > "$batch"
    if sftp -q -b "$batch" -i "$identity" -oBatchMode=yes -oIdentitiesOnly=yes \
        -oStrictHostKeyChecking=yes -oUserKnownHostsFile="$known_hosts" \
        -P "$port" "$user@$host" >/dev/null 2>&1; then
        fail "the remote backup already exists"
    fi
    printf 'put %s %s\nls -ln %s\n' "$archive" "$remote_partial" "$remote_partial" > "$batch"
    sftp -q -b "$batch" -i "$identity" -oBatchMode=yes -oIdentitiesOnly=yes \
        -oStrictHostKeyChecking=yes -oUserKnownHostsFile="$known_hosts" \
        -P "$port" "$user@$host" > "$listing" 2>/dev/null || fail "SFTP upload failed"
    remote_size=$(awk -v path="$remote_partial" '$NF == path { value=$5 } END { print value }' "$listing")
    [ "$remote_size" = "$local_size" ] || fail "SFTP remote size does not match"
    printf 'rename %s %s\n' "$remote_partial" "$remote_final" > "$batch"
    sftp -q -b "$batch" -i "$identity" -oBatchMode=yes -oIdentitiesOnly=yes \
        -oStrictHostKeyChecking=yes -oUserKnownHostsFile="$known_hosts" \
        -P "$port" "$user@$host" >/dev/null 2>&1 || fail "SFTP final rename failed"
    remote_partial=""
    rm -f "$batch" "$listing"
    trap - EXIT HUP INT TERM
}

transfer_webdav() {
    unknown=$(grep -Ev '^(TRANSFER_TARGET|WEBDAV_BASE_URL|WEBDAV_CREDENTIAL_FILE)=[^[:cntrl:]]+$|^[[:space:]]*(#|$)' "$CONFIG_FILE" || true)
    [ -z "$unknown" ] || fail "$CONFIG_FILE contains unknown or invalid WebDAV entries"
    base_url=$(read_value "$CONFIG_FILE" WEBDAV_BASE_URL)
    credentials=$(read_value "$CONFIG_FILE" WEBDAV_CREDENTIAL_FILE)
    case "$base_url" in https://*) ;; *) fail "WEBDAV_BASE_URL must use HTTPS";; esac
    case "${base_url#https://}" in *\?*|*#*|*@*|'') fail "WEBDAV_BASE_URL is invalid";; esac
    base_url=${base_url%/}
    require_private_file "$credentials" "WebDAV credential file"
    command -v curl >/dev/null 2>&1 || fail "curl is required"
    final_url="$base_url/$filename"
    partial_url="$base_url/$partial_name"
    headers=$(mktemp)
    cleanup_webdav() {
        curl --silent --output /dev/null --request DELETE --netrc-file "$credentials" \
            --proto '=https' --tlsv1.2 "$partial_url" || true
        rm -f "$headers"
    }
    trap cleanup_webdav EXIT HUP INT TERM
    status=$(curl --silent --output /dev/null --write-out '%{http_code}' --head \
        --netrc-file "$credentials" --proto '=https' --tlsv1.2 "$final_url") || fail "WebDAV availability check failed"
    case "$status" in 404) ;; 2??) fail "the remote backup already exists";; *) fail "WebDAV availability check returned HTTP $status";; esac
    curl --fail --silent --show-error --output /dev/null --upload-file "$archive" \
        --header 'If-None-Match: *' --netrc-file "$credentials" --proto '=https' \
        --tlsv1.2 "$partial_url" || fail "WebDAV upload failed"
    curl --fail --silent --show-error --head --dump-header "$headers" --output /dev/null \
        --netrc-file "$credentials" --proto '=https' --tlsv1.2 "$partial_url" || fail "WebDAV size check failed"
    remote_size=$(sed -n 's/^[Cc]ontent-[Ll]ength:[[:space:]]*\([0-9][0-9]*\)\r*$/\1/p' "$headers" | tail -n 1)
    [ "$remote_size" = "$local_size" ] || fail "WebDAV remote size does not match"
    curl --fail --silent --show-error --output /dev/null --request MOVE \
        --header "Destination: $final_url" --header 'Overwrite: F' \
        --netrc-file "$credentials" --proto '=https' --tlsv1.2 "$partial_url" || fail "WebDAV final MOVE failed"
    partial_url=""
    rm -f "$headers"
    trap - EXIT HUP INT TERM
}

case "$target" in
    sftp) transfer_sftp;;
    webdav) transfer_webdav;;
    *) fail "TRANSFER_TARGET must be sftp or webdav";;
esac

printf '%s\n' "Backup transferred: $filename"
