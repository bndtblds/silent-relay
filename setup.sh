#!/bin/sh
set -eu

if [ -e .env ]; then
    echo "Setup stopped: .env already exists and will not be overwritten." >&2
    exit 2
fi

docker version >/dev/null
docker compose --profile setup build setup
docker compose --profile setup run --rm \
    --user "$(id -u):$(id -g)" \
    setup

docker compose up -d --wait --wait-timeout 120
docker compose ps

printf '\n%s\n' "SilentRelay is running. Open the HTTPS address configured during setup."
printf '%s\n' "Sign in at /admin/login and configure SMTP under system settings."
