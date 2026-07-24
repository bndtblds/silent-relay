# SilentRelay

SilentRelay is a self-hosted private notification service for trusted contacts. A trusted person can submit a confidential message through a secret QR link without learning who receives it, how many recipients exist, or which relationships and contact details are stored.

SilentRelay is not an emergency number, medical alarm, messenger, or guaranteed high-availability service.

## Implemented features

- Account creation with a single-use setup link and a reusable account-owner QR/link that is displayed only once
- Account-owner authentication using a 256-bit secret token plus an Argon2id password
- Server-side, revocable account-owner and admin sessions with session-bound CSRF protection
- Encrypted owner and partner contact values, partner names, display names, temporary messages, and provider error details
- Email verification and account activation
- Account-owner and partner contact management, with trusted persons assignable to either
- Locally generated QR codes and immediate token rotation/revocation
- Printable one-time account-owner access sheet with QR code and security guidance
- Two-step public message submission with one-time deduplication
- Server-side recipient selection: every active group member except the account owner or partner assigned to the submitting trusted person
- SMTP provider abstraction, bounded exponential retry, and message erasure after successful delivery
- Review reminders, overdue/disabled/deletion lifecycle, pending-account cleanup, session cleanup, and audit retention
- Anonymous admin account overview and technical health information
- Encrypted runtime SMTP configuration, connection checks, and test-email delivery through the admin UI
- SQLite with WAL mode, SQLAlchemy, Alembic, a separate scheduler container, and Docker Compose
- Restrictive security headers, neutral token failures, rate limiting, JSON technical logs, and disabled HTTP access logs

The accepted architecture decisions are in `docs.local/adr/` in the working specification. The runtime threat model is in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## Requirements

- Docker Engine with Docker Compose, or Python 3.12+
- An SMTP account
- HTTPS termination through a reverse proxy for production

## Configuration

Copy `.env.example` to `.env` and fill every secret and SMTP value. Never commit `.env`.

Generate a field-encryption key:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Generate four independent random values of at least 32 bytes for `TOKEN_HMAC_KEY`, `FINGERPRINT_HMAC_KEY`, `SESSION_SECRET`, and `CSRF_SECRET`. Generate the admin password hash after installing dependencies:

```sh
python -c "from app.security.core import hash_password; print(hash_password(input('Password: ')))"
```

`APP_BASE_URL` must be the public HTTPS origin in production. Cookies are Secure by default. `TRUSTED_PROXY_COUNT` is reserved for deployment policy; the application intentionally uses the direct peer address and Uvicorn accepts forwarded headers only from the configured reverse-proxy address.

Secrets should be injected by the host's secret manager or Docker secrets through an entrypoint that maps secret files to environment variables. Secrets must remain separate from the database backup.

## Start with Docker Compose

```sh
docker compose build
docker compose run --rm migrate
docker compose up -d web scheduler
docker compose ps
```

Only expose `web` through an HTTPS reverse proxy. The proxy must not log paths beginning with `/account/`, `/notify/`, or `/verify-contact/`, because those paths contain bearer secrets. It must overwrite, not append, forwarding headers. Run exactly one scheduler instance with SQLite.

Health endpoints:

- `GET /health/live` checks the process.
- `GET /health/ready` checks database connectivity and loaded application configuration.

SMTP is intentionally not a readiness dependency.

After signing in as admin, open `/admin/system` to configure SMTP, test the connection, and send a test email. Values entered there override the SMTP environment variables for both web requests and scheduler jobs. SMTP credentials are encrypted with `FIELD_ENCRYPTION_KEY`; the password is never rendered back into the browser.

## Local development and tests

```sh
python -m venv .venv
.venv/Scripts/pip install -e ".[test]"
.venv/Scripts/alembic upgrade head
.venv/Scripts/pytest
.venv/Scripts/uvicorn app.main:app --reload --no-access-log
```

For local HTTP development only, set `APP_ENV=test`, `APP_BASE_URL=http://localhost:8000`, `SECURE_COOKIES=false`, and `HSTS_ENABLED=false`. Never use these values in production.

## Operation

The scheduler retries temporary delivery errors after approximately 1 minute, 5 minutes, 30 minutes, 2 hours, 12 hours, and 24 hours, bounded by `DELIVERY_MAX_ATTEMPTS`. Permanent recipient rejection stops retrying. Once all deliveries succeed, the encrypted message payload is erased. Delivery metadata and a keyed message digest remain.

Review reminder offsets accept unsorted and duplicate comma-separated integers; configuration normalizes them. Negative values are before the due date, zero is the due date, and positive values are after it.

There is no password or account-owner token recovery in version 1. Losing either factor can make an account inaccessible. Store the account-owner QR/link and password securely and separately.

### Backup

1. Stop `web` and `scheduler`, or take a storage-level consistent SQLite snapshot.
2. Back up the database volume.
3. Back up the Alembic version and deployment configuration without unnecessary secrets.
4. Back up encryption and HMAC keys separately from the database.
5. Encrypt backup media and test restoration regularly.

A database backup without its field-encryption key is intentionally unusable for encrypted fields.

### Restore

1. Stop all services.
2. Restore the database volume.
3. Restore the exact encryption, fingerprint, token, session, and CSRF secrets.
4. Run `docker compose run --rm migrate`.
5. Start `web`, check `/health/ready`, then start the single `scheduler`.
6. Perform a test notification through a dedicated test account.

### Update

1. Make and verify a backup.
2. Pull or build the reviewed release image.
3. Stop `web` and `scheduler`.
4. Run the migration service.
5. Start `web`, verify readiness, then start `scheduler`.
6. Check technical logs for migration or delivery failures.

Never downgrade the database unless the target release explicitly documents a supported downgrade.

### Field-key rotation

Version 1 uses a single Fernet field key. Rotation is an offline maintenance operation:

1. Stop both services and create a verified backup.
2. Keep the old key available only for the maintenance window.
3. In one controlled migration, decrypt every encrypted column with the old key and immediately encrypt it with the new key.
4. Commit in small transactions while recording only technical progress, never plaintext.
5. Start with the new key and verify representative records and health checks.
6. On failure, stop and restore both database and old key from the matched backup.
7. Destroy obsolete key copies according to the operator's retention policy.

Do not start the application with a new key before data has been re-encrypted.

## Privacy notes

The application does not log request bodies, messages, names, email addresses, cookies, or clear tokens. Uvicorn access logs are disabled because secret tokens occur in paths. Audit records contain only event names, anonymous account IDs, request IDs where available, and allow-listed technical metadata.

The admin UI displays anonymous account IDs, state, timestamps, and aggregate technical failures only. A host administrator remains inside the trust boundary because runtime memory and secrets can reveal data.

## License

See [LICENSE](LICENSE).
