#!/bin/sh
set -eu

source_root=$(pwd)
test_root=$(mktemp -d "${TMPDIR:-/tmp}/silentrelay-backup-integration.XXXXXX")
source_copy="$test_root/source"
restore_copy="$test_root/restore"
backup_directory="$test_root/backups"
identity_file="$test_root/age-identity.key"
source_project="srbackupsource$$"
restore_project="srbackuprestore$$"

cleanup() {
    COMPOSE_PROJECT_NAME=$source_project docker compose -f "$source_copy/docker-compose.yml" \
        -f "$source_copy/docker-compose.override.yml" down --volumes --remove-orphans --rmi local >/dev/null 2>&1 || true
    COMPOSE_PROJECT_NAME=$restore_project docker compose -f "$restore_copy/docker-compose.yml" \
        -f "$restore_copy/docker-compose.override.yml" down --volumes --remove-orphans --rmi local >/dev/null 2>&1 || true
    docker network rm "${source_project}_default" "${restore_project}_default" >/dev/null 2>&1 || true
    rm -rf "$test_root"
}
trap cleanup EXIT HUP INT TERM

fail() {
    printf '%s\n' "Integration test failed: $*" >&2
    exit 1
}

write_override() {
    target=$1
    cp "$source_root/backup.sh" "$source_root/restore.sh" "$target/"
    cp "$source_root/app/backup_restore.py" "$target/app/backup_restore.py"
    cat > "$target/docker-compose.override.yml" <<'EOF'
services:
  caddy:
    profiles: [production]
EOF
}

prepare_copy() {
    target=$1
    git clone --quiet --no-local "$source_root" "$target"
    write_override "$target"
}

prepare_environment() {
    target=$1
    project=$2
    cd "$target"
    COMPOSE_PROJECT_NAME=$project docker compose --profile setup build setup >/dev/null
    printf '%s\n' localhost.test '' admin SilentRelay-Integration-2026! SilentRelay-Integration-2026! | \
        COMPOSE_PROJECT_NAME=$project docker compose --profile setup run --rm -T --user root setup >/dev/null
    sed -i \
        -e 's|^APP_ENV=.*|APP_ENV=test|' \
        -e 's|^APP_BASE_URL=.*|APP_BASE_URL=http://localhost|' \
        -e 's|^HSTS_ENABLED=.*|HSTS_ENABLED=false|' \
        -e 's|^SECURE_COOKIES=.*|SECURE_COOKIES=false|' .env
}

prepare_copy "$source_copy"
prepare_environment "$source_copy" "$source_project"
age-keygen -o "$identity_file" >/dev/null
recipient=$(age-keygen -y "$identity_file")
mkdir -p "$backup_directory"
cat > "$source_copy/.backup.conf" <<EOF
BACKUP_DIRECTORY=$backup_directory
KEEP_BACKUPS=7
AGE_RECIPIENT=$recipient
INSTALLATION_ID=11111111-1111-1111-1111-111111111111
EOF

cd "$source_copy"
COMPOSE_PROJECT_NAME=$source_project docker compose up -d --wait web scheduler >/dev/null
source_database_hash=$(COMPOSE_PROJECT_NAME=$source_project docker compose exec -T web \
    python -c "import hashlib; print(hashlib.sha256(open('/data/app.db','rb').read()).hexdigest())")

# Run enough complete backups to verify retention beyond the configured seven.
backup_number=1
while [ "$backup_number" -le 8 ]; do
    COMPOSE_PROJECT_NAME=$source_project sh backup.sh >/dev/null
    backup_number=$((backup_number + 1))
    [ "$backup_number" -gt 8 ] || sleep 1
done
backup_count=$(find "$backup_directory" -maxdepth 1 -name '*.tar.gz.age' | wc -l | tr -d ' ')
[ "$backup_count" -eq 7 ] || fail "retention kept $backup_count backups instead of 7"
selected_backup=$(find "$backup_directory" -maxdepth 1 -name '*.tar.gz.age' | sort | head -n 1)

# An encryption failure must leave no partial archive and restart prior services.
real_age=$(command -v age)
mkdir "$test_root/failing-bin"
cat > "$test_root/failing-bin/age" <<EOF
#!/bin/sh
if [ "\${1:-}" = "-r" ]; then exit 42; fi
exec "$real_age" "\$@"
EOF
chmod +x "$test_root/failing-bin/age"
if PATH="$test_root/failing-bin:$PATH" COMPOSE_PROJECT_NAME=$source_project sh backup.sh >/dev/null 2>&1; then
    fail "backup succeeded although encryption was forced to fail"
fi
[ -z "$(find "$backup_directory" -maxdepth 1 -name '*.partial' -print -quit)" ] || \
    fail "failed encryption left a partial archive"
for service in web scheduler; do
    COMPOSE_PROJECT_NAME=$source_project docker compose ps --status running --services | \
        grep -qx "$service" || fail "$service was not restarted after backup failure"
done

# A replacement safety backup must not remove the selected restore archive.
printf '%s\n' REPLACE | COMPOSE_PROJECT_NAME=$source_project sh restore.sh --replace \
    "$selected_backup" "$identity_file" >/dev/null
[ -f "$selected_backup" ] || fail "pre-restore retention removed the selected archive"

prepare_copy "$restore_copy"
cd "$restore_copy"
cat > docker-compose.override.yml <<'EOF'
services:
  caddy:
    profiles: [production]
EOF

# An unrelated Git history must be rejected before configuration or data is written.
git checkout --quiet --orphan incompatible
git config user.name "SilentRelay integration test"
git config user.email "integration@example.invalid"
git add .
git commit --quiet -m incompatible
if printf '%s\n' RESTORE | COMPOSE_PROJECT_NAME=$restore_project sh restore.sh \
    "$selected_backup" "$identity_file" >/dev/null 2>&1; then
    fail "restore accepted incompatible Git history"
fi
[ ! -e .env ] || fail "compatibility rejection wrote .env"

git checkout --quiet --detach "$(git -C "$source_copy" rev-parse HEAD)"
write_override "$restore_copy"
printf '%s\n' RESTORE | COMPOSE_PROJECT_NAME=$restore_project sh restore.sh \
    "$selected_backup" "$identity_file" >/dev/null
restored_database_hash=$(COMPOSE_PROJECT_NAME=$restore_project docker compose exec -T web \
    python -c "import hashlib; print(hashlib.sha256(open('/data/app.db','rb').read()).hexdigest())")
[ "$source_database_hash" = "$restored_database_hash" ] || fail "restored database differs"
cmp "$source_copy/.env" "$restore_copy/.env" >/dev/null || fail "restored .env differs"
COMPOSE_PROJECT_NAME=$restore_project docker compose exec -T web \
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)" || \
    fail "restored web service is not ready"

printf '%s\n' "Docker backup and restore integration test passed."
