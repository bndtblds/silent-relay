# Operating SilentRelay

This document contains the technical details required to deploy and maintain a
SilentRelay installation. Start with the project [README](../README.md) for the
short installation guide.

## Install Docker on Debian 13

SilentRelay requires Docker Engine with the Docker Compose plugin. The following
example follows Docker's official installation method for Debian 13
(`trixie`). Run these commands as `root`; otherwise prefix administrative
commands with `sudo`.

Install the repository prerequisites and Docker signing key:

```sh
apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
```

Add Docker's stable package repository:

```sh
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: trixie
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
```

Install and verify Docker:

```sh
apt-get update
apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

systemctl enable --now docker
docker version
docker compose version
docker run --rm hello-world
```

For other operating systems, use the
[official Docker Engine installation guide](https://docs.docker.com/engine/install/).
Docker-published container ports can interact with host firewall rules; use the
provider firewall as an additional boundary and expose only the ports required
by SilentRelay.

Create the application directory and clone the repository:

```sh
install -d /opt/silent-relay
git clone https://github.com/bndtblds/silent-relay.git /opt/silent-relay
cd /opt/silent-relay
```

## Configuration

The recommended installation uses `setup.sh` on Linux or `setup.ps1` on
Windows. The setup creates `.env`, generates independent secrets, hashes the
administrator password, starts all services, and checks application readiness.
It refuses to overwrite an existing `.env`.

For a manual installation, copy `.env.example` to `.env` and fill every
required value. Never commit `.env`.

`CADDY_DOMAIN` is the primary public domain without a scheme or path.
`CADDY_DOMAINS` contains the primary domain and every optional additional
domain, separated by commas. `APP_BASE_URL` is the matching public HTTPS origin
for the primary domain. SilentRelay generates links only with this primary
origin. Caddy obtains a certificate for every configured domain and redirects
additional domains to the primary domain. Cookies are secure by default, so
plain HTTP is not suitable for production.

Every configured domain must have a working DNS record pointing to the server.
Wildcard domains are not supported by the guided setup because they require
DNS-based ACME validation and provider credentials.

Generate the field-encryption key:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Generate four different random values of at least 32 bytes for:

- `TOKEN_HMAC_KEY`
- `FINGERPRINT_HMAC_KEY`
- `SESSION_SECRET`
- `CSRF_SECRET`

After installing the project dependencies, generate the administrator password
hash:

```sh
python -c "from app.security.core import hash_password; print(hash_password(input('Password: ')))"
```

Store the result in `ADMIN_PASSWORD_HASH`. Store only the hash, never the clear
password.

Secrets should be supplied by the host's secret manager or Docker secrets where
possible. They must be backed up separately from the database.

## SMTP

After signing in at `/admin/login`, open `/admin/system`. SMTP settings can be
entered, checked, and tested there. Settings saved in the administration area
override SMTP environment variables for both the web service and scheduler.

SMTP credentials are encrypted with `FIELD_ENCRYPTION_KEY`. The saved password
is never displayed in the browser.

SMTP is intentionally not part of the readiness check. A temporary SMTP failure
must not make the web application unavailable.

## HTTPS and reverse proxy

The included Caddy service is the only publicly exposed service. It listens on
ports `80` and `443`, obtains a publicly trusted TLS certificate, redirects
HTTP to HTTPS, and forwards requests to `web` over an internal Docker network.
Caddy stores certificate state in the `caddy-data` and `caddy-config` volumes.

Before starting SilentRelay:

- Point the configured public domain to the server.
- Allow inbound TCP traffic on ports `80` and `443`.
- Allow inbound UDP traffic on port `443` for HTTP/3, if supported.

Caddy access logging is not enabled, and Uvicorn access logs are disabled,
because account, notification, and verification paths can contain secret
tokens. The `web` service has no published host port and accepts forwarding
headers only inside the trusted Compose deployment.

Operators who replace Caddy must preserve these properties: HTTPS, no logging
of secret-bearing paths, overwritten forwarding headers, and no direct public
access to `web`.

## Health checks

- `GET /health/live` confirms that the process is running.
- `GET /health/ready` confirms that configuration is loaded and the database is
  reachable.

Check the running services with:

```sh
docker compose ps
docker compose logs caddy web scheduler
```

Application logs contain technical events but intentionally exclude messages,
names, email addresses, cookies, and clear access tokens.

## Scheduler and delivery

Run exactly one scheduler instance with SQLite. It sends queued messages,
retries temporary delivery failures, removes delivered message content, sends
account-review reminders, and performs retention cleanup.

Permanent recipient rejection is not retried. Temporary failures use bounded
retries controlled by `DELIVERY_MAX_ATTEMPTS`. Encrypted message content is
removed after all deliveries succeed or after its retention period expires.

## Backup

1. Stop `web` and `scheduler`, or create a storage-level consistent SQLite
   snapshot.
2. Back up the `silentrelay-data` volume.
3. Back up the `caddy-data` and `caddy-config` volumes to preserve certificate
   state.
4. Back up deployment configuration without adding unnecessary copies of
   secrets.
5. Back up the encryption, HMAC, session, and CSRF secrets separately from the
   database.
6. Encrypt backup media and test restoration regularly.

A database backup without the matching field-encryption key is intentionally
unusable for encrypted fields.

## Restore

1. Stop all SilentRelay services.
2. Restore the database volume.
3. Restore the exact encryption, HMAC, session, and CSRF secrets that belong to
   that database.
4. Run:

   ```sh
   docker compose run --rm migrate
   ```

5. Start the deployment with `docker compose up -d`.
6. Verify `/health/ready` and the public HTTPS address.
7. Send a test notification through a dedicated test account.

## Update

1. Create and verify a backup.
2. Pull the reviewed release or build the new image.
3. Stop `caddy`, `web`, and `scheduler`.
4. Run the migration service:

   ```sh
   docker compose run --rm migrate
   ```

5. Start the deployment with `docker compose up -d`.
6. Verify the public HTTPS address and check the service logs.

Do not downgrade the database unless the target release explicitly documents a
supported downgrade.

## Access loss

SilentRelay does not provide password or account-owner access recovery. Losing
either factor can make an account inaccessible. Store the printable
account-owner access and its password securely and separately.

## Field-encryption key rotation

Version 1 uses one Fernet field-encryption key. Rotation is an offline
maintenance operation and requires a purpose-built data migration:

1. Stop both services and create a verified backup.
2. Keep the old key available only during the maintenance window.
3. Decrypt each encrypted field with the old key and immediately encrypt it
   with the new key.
4. Start SilentRelay with the new key and verify representative data.
5. Restore the matched database and old key if verification fails.
6. Securely remove obsolete key copies after a successful rotation.

Never start the existing database with a new key before its encrypted data has
been re-encrypted.

## Production checklist

- HTTPS is enforced.
- The public domain points to the server and Caddy can bind ports 80 and 443.
- `.env` and all backups are protected.
- Secret-bearing URL paths are excluded from proxy logs.
- Only Caddy is publicly exposed.
- Exactly one scheduler is running with SQLite.
- SMTP test delivery succeeds.
- `/health/ready` succeeds.
- Backup restoration has been tested.
