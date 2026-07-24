$ErrorActionPreference = "Stop"

if (Test-Path -LiteralPath ".env") {
    throw "Setup stopped: .env already exists and will not be overwritten."
}

docker version | Out-Null
docker compose --profile setup build setup
docker compose --profile setup run --rm --user root setup

docker compose up -d --wait --wait-timeout 120
docker compose ps

Write-Host ""
Write-Host "SilentRelay is running. Open the HTTPS address configured during setup."
Write-Host "Sign in at /admin/login and configure SMTP under system settings."
