# Developing SilentRelay

This guide explains the normal development workflow for SilentRelay. For
production installation and maintenance, use [OPERATIONS.md](OPERATIONS.md).
The optional, narrowly scoped registration entitlement interface is documented
in [ENTITLEMENTS.md](ENTITLEMENTS.md).

## Requirements

Install:

- Git;
- Python 3.12 or newer; and
- [uv](https://docs.astral.sh/uv/getting-started/installation/).

Docker Engine with Docker Compose is optional for ordinary Python development.
It is required when changing the container image, Compose deployment, Caddy
configuration, or guided production setup.

Check the tools:

```sh
git --version
python --version
uv --version
```

Run all commands from the repository root.

## Install the development environment

Create the project environment and install the locked dependencies, including
the test tools:

```sh
uv sync --locked --extra test
```

`uv` creates and manages `.venv` automatically. Use `uv run` for project
commands instead of platform-specific paths such as `.venv/Scripts` or
`.venv/bin`.

The production image also installs from `uv.lock`; update and verify the
lockfile deliberately whenever project dependencies change.

## Run the tests first

The quickest way to verify the working tree is:

```sh
uv run pytest
```

The tests create isolated application settings and databases. They do not need
a production `.env` file or a working SMTP account.

The real backup and restore workflow is a separate Docker integration test. It
requires Docker Compose, `age`, `age-keygen`, Git, and a POSIX shell. It creates
isolated Compose projects and volumes and removes them when finished:

```sh
SILENTRELAY_RUN_DOCKER_INTEGRATION=1 uv run pytest tests/integration/test_backup_restore_docker.py
```

This test covers the complete shell paths, service stop and restart, retention,
encryption failure, selected-archive protection, Git compatibility rejection,
restore, migration, readiness, and data comparison. Do not include it in every
fast unit-test run.

Run one test file:

```sh
uv run pytest tests/test_http.py
```

Run one test by name:

```sh
uv run pytest tests/test_http.py::test_public_html_pages_render
```

Show coverage when needed:

```sh
uv run pytest --cov=app --cov-report=term-missing
```

Pull requests and pushes to `main` must pass the separate Tests workflow. It
installs the locked test environment, runs the complete normal test suite with
branch coverage, validates the canonical and release-tag versions, and
publishes the HTML and XML coverage reports plus the commit and workflow-run
identity as a workflow artifact. The measured coverage is reported but is not
subject to an arbitrary minimum threshold.

The opt-in Docker backup and restore integration test remains separate from
this required gate because it needs additional host tools and performs a
heavyweight deployment exercise. Migration tests remain part of the normal
required test run.

## Run SilentRelay locally

This section is only needed for browser-based development. Automated tests are
the preferred path for most changes.

Copy the configuration template:

On Linux or macOS:

```sh
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Change these values in `.env`:

```text
APP_ENV=test
APP_BASE_URL=http://localhost:8000
DATABASE_URL=sqlite:///./data/development.db
DEFAULT_LANGUAGE=en
HSTS_ENABLED=false
SECURE_COOKIES=false
```

Generate the field-encryption key and four independent secrets:

```sh
uv run python -c "from cryptography.fernet import Fernet; import secrets; print('FIELD_ENCRYPTION_KEY=' + Fernet.generate_key().decode()); [print(name + '=' + secrets.token_urlsafe(48)) for name in ('TOKEN_HMAC_KEY', 'FINGERPRINT_HMAC_KEY', 'SESSION_SECRET', 'CSRF_SECRET')]"
```

Copy the five generated lines into the matching empty fields in `.env`.

Generate a local administrator password hash:

```sh
uv run python -c "from app.setup import hash_admin_password; import getpass; print(hash_admin_password(getpass.getpass('Developer administrator password: ')))"
```

Store the result in `ADMIN_PASSWORD_HASH` and surround it with single quotes:

```text
ADMIN_PASSWORD_HASH='$argon2id$...'
```

The generated `.env` is ignored by Git. Never commit it, reuse it in
production, or copy production secrets into a development environment.

Initialize the local database:

```sh
uv run alembic upgrade head
```

Start the web application:

```sh
uv run uvicorn app.main:app --reload --no-access-log
```

Open:

```text
http://localhost:8000
```

The technical administration is available at:

```text
http://localhost:8000/admin/login
```

Start the scheduler in a second terminal only when testing scheduled delivery,
account review, or cleanup:

```sh
uv run python -m app.scheduler.main
```

The scheduler keeps running until it is stopped with `Ctrl+C`. Do not start
multiple schedulers against the same development database.

By default, local SMTP delivery will fail if no SMTP server is available. In
the test environment, SilentRelay displays a failed verification link to the
current setup browser so that the manual account flow can continue.

## Project structure

- `app/main.py` creates the FastAPI application and security middleware.
- `app/i18n.py` contains language negotiation, formatting, and the small
  German/English translation catalog.
- `app/public_markdown.py` renders the deliberately limited Markdown accepted
  for public operator information.
- `app/routers/` contains public, account-owner, partner, trusted-person, and
  technical-admin HTTP routes. The main web routes are split by access domain
  under `app/routers/web/` and retain a shared router through `app.routers.web`.
- `app/services/` contains application rules and database operations, split by
  focused domains while retaining stable imports through `app.services`.
- `app/models.py` contains the SQLAlchemy database model.
- `app/providers/` contains the notification-provider interface and SMTP
  implementation.
- `app/email_tracking.py` creates privacy-preserving envelope correlation and
  handles both immediate permanent SMTP rejection and standards-compliant
  delivery-status reports received later over IMAP. Both paths must produce the
  same contact and delivery state.
- `app/scheduler/` runs delivery, per-contact confirmation, account review,
  recurring contact-problem reminders, lifecycle, and cleanup jobs.
- `app/templates/` and `app/static/` contain the server-rendered user
  interface.
- `migrations/` contains Alembic database migrations.
- `tests/` contains HTTP, service, scheduler, security, SMTP, and setup tests.

Keep HTTP handling in routers and business rules in the service layer. Reuse
the existing provider, session, encryption, and database abstractions instead
of duplicating them.

Use `app.time.utc_now()` for the current time and keep application datetimes
timezone-aware in UTC. Persisted model timestamps use `UTCDateTime`, which
stores normalized naive UTC values for SQLite compatibility and restores UTC
timezone information when values are loaded. Do not use `datetime.utcnow()` or
interpret a naive datetime as local time.

The regular review has two independent confirmations. `AccountReview`
records the account owner's review of people and assignments.
`ContactReview` records confirmation of each active contact method.
`ContactReviewToken` stores only the keyed hash of each one-time link, so a
later reminder does not invalidate an earlier unused link.
The scheduler creates contact-review rows only when the review becomes due,
sends only hashed one-time tokens, and uses the configured grace deadline.
The account advances to its next review only when both parts are complete.
Never place the secret account-owner access token in a reminder email.

Contact-confirmation links deliberately use two HTTP steps. GET renders only a
neutral page and creates a short-lived public session; it must never confirm a
contact or consume a token. Only the session-bound, CSRF-protected POST may
atomically consume an initial or periodic confirmation token. Tests for this
flow must include automatic GET requests, replay, expiry, wrong CSRF values,
and parallel POST attempts.

Each accepted notification stores a fixed `release_at` calculated from the
global waiting period at submission time. Later configuration changes must not
alter an existing release time. Before release, the authenticated trusted
person can cancel the notification atomically. Cancellation sets
`cancelled_at`, erases the encrypted payload, and changes every delivery that
has not started from `pending` to `cancelled`.

Initial delivery and every scheduler retry use `DeliveryService`. Its candidate
selection and conditional claim both require a released, non-cancelled
notification. Before a provider call it also rechecks the notification,
payload, expiry, account and lock, contact verification and ownership, and any
partner assignment. A locally withdrawn delivery becomes `cancelled`; this is
distinct from an external `permanent_failure`.

Delivery processing uses an atomic conditional claim and a two-minute lease in
`processing_started_at` and `processing_until`. A live lease cannot be claimed
again. An expired lease is recoverable after a process restart and must repeat
the complete authorization check. All normal terminal or retry outcomes clear
the lease. Keep crash-before-provider, crash-after-SMTP, overlapping-cycle,
expired-lease, and migration tests when changing this code. SMTP can still
produce a duplicate after acceptance followed by a crash; ADR 0013 remains the
authoritative limitation.

The crash-after-SMTP regression test injects process failure only in the test,
immediately after a successful provider result and before the local success
commit. It must prove that the committed claim prevents another provider call
while its lease is live, that recovery after lease expiry repeats the complete
send-time authorization path, and that the second attempt can finish normally.
Both provider calls must receive exactly the localized neutral notice and no
confidential message text, names, relationships, roles, recipient count, secret
access data, or other message context. This test protects the deliberately
accepted at-least-once boundary; it does not establish or imply SMTP
exactly-once delivery.

### Measuring the SQLite write lock during SMTP delivery

`test_slow_smtp_holds_sqlite_write_lock_until_release_or_timeout` is a
controlled integration test for the current delivery transaction boundary. It
uses a file-backed SQLite database in WAL mode, separate sessions and
connections for the delivery worker and an unrelated configuration write, and
a provider held by events after the SMTP send path has been entered. Durations
are measured with `time.monotonic()`.

The test engine deliberately uses explicit SQLite busy timeouts instead of an
environment-dependent default. The successful case uses a generous five-second
timeout and a separate short observation window before releasing the provider;
this proves that the unrelated writer was blocked while SMTP remained blocked,
then committed after the delivery transaction completed without coupling the
expected success to a narrow timing margin. The timeout case keeps the provider
blocked beyond a deliberately short 0.25-second timeout and proves that the
writer fails with SQLite's `database is locked` error after approximately that
timeout, while releasing the provider afterward still lets delivery finish
normally.

Run the measurement with:

```sh
uv run pytest tests/test_scheduler.py::test_slow_smtp_holds_sqlite_write_lock_until_release_or_timeout -vv
```

This establishes lock causality, not a cross-platform performance guarantee:
the delivery transaction holds a SQLite write lock across external SMTP I/O,
so an unrelated writer waits for the remaining SMTP duration or reaches its
configured SQLite timeout. Whether that behavior is practically relevant for
SilentRelay's small intended workload remains a separate operational decision.
The measurement does not justify an outbox, another database, or changed
transaction boundaries by itself.

Queue tests must cover the default and configured delay, unchanged release
times after configuration changes, no provider call before release, delivery
at or after release, cancellation by the matching trusted person, rejection at
the release boundary or after processing starts, payload erasure, and migration
of existing notifications as immediately due.

Public operator information is untrusted input even though only the technical
admin can edit it. Keep its Markdown renderer allowlist-based: escape text and
attributes, preserve source line breaks, permit only the documented formatting
and URL schemes, and never enable raw HTML, images, embeds, or arbitrary
Markdown extensions. Add both positive rendering tests and negative injection
tests when changing it.

## Database changes

When changing persisted data:

1. Update `app/models.py`.
2. Create an Alembic migration:

   ```sh
   uv run alembic revision --autogenerate -m "Describe the schema change"
   ```

3. Read the generated migration carefully. Autogeneration is a starting point,
   not proof that the migration is safe.
4. Apply it to the development database:

   ```sh
   uv run alembic upgrade head
   ```

5. Add tests for both the new behavior and important boundary cases.

## Languages

German (`de`) and English (`en`) are supported. Public and technical-admin
requests negotiate `Accept-Language`; account-specific requests always use the
language stored on the account. Keep secret URLs language-neutral. A new
language is complete only when all user-facing templates, emails, validation
messages, status labels, accessibility text, and representative HTTP flows are
covered.

Never edit an existing production database manually. Do not remove or rewrite
an already published migration merely to make a local development copy work.

## Working with Docker

Docker is not required for every code change. Use it when the deployment itself
is affected.

Validate the Compose configuration:

```sh
docker compose config --quiet
```

Build the application image:

```sh
docker compose build
```

Run the full deployment only with a suitable `.env` and domains. The production
setup enables secure cookies and HTTPS; the local HTTP settings above are not a
production configuration.

## Before committing

Advance the canonical version in `app/version.py` once per pull request. Keep
that target version unchanged for later commits in the same pull request and
refresh `uv.lock` if needed. Validate it against `origin/main`:

```sh
uv run python scripts/check_version.py
```

The complete versioning and release rules are documented in
[Versioning and releases](VERSIONING.md).

Run the checks relevant to the change. For ordinary application changes:

```sh
uv run pytest
git diff --check
```

GitHub Actions also runs a weekly and change-triggered Trivy security workflow.
It scans the locked Python dependencies, the built application image, and the
digest-pinned Caddy image. The workflow publishes SARIF results to GitHub code
scanning and retains matching CycloneDX SBOMs plus scan metadata as workflow
artifacts for 90 days. A fixable `HIGH` or `CRITICAL` vulnerability in the
locked dependencies or application image fails the workflow. Equivalent
findings in the separately maintained upstream Caddy image produce a visible
warning until an updated official image is available. All findings remain in
the reports.

Review all three scan targets when changing dependencies or containers. Do not
silence a finding without a documented reason and a time-bounded follow-up.
Action references and scanner versions are deliberately pinned and must be
updated through reviewed dependency updates rather than replaced with floating
`latest` references.

Also build or validate Docker when changing:

- `Dockerfile`;
- `docker-compose.yml`;
- `Caddyfile`;
- `setup.sh` or `setup.ps1`;
- `.env.example`; or
- production dependencies.

Review `git status` and the complete diff before staging files. Do not commit
local databases, `.env`, caches, generated coverage reports, or unrelated user
changes.

## Privacy and security rules

SilentRelay handles confidential messages and contact details. Changes must
preserve these rules:

- never log message text, contact details, passwords, cookies, or clear access
  tokens;
- never store or log inbound email bodies, attachments, returned messages, or
  addresses found in delivery reports;
- never expose recipient identities or recipient counts to a trusted person;
- never place confidential message text, the person concerned, relationships,
  recipient counts, or secret account/partner access in notification emails;
- keep domain-level inbox recipients and `read_at` separate from SMTP delivery
  state, and fix recipients only at release time;
- keep sensitive database fields encrypted;
- hash authentication tokens instead of storing them in clear text;
- keep account-owner and technical-admin sessions separate;
- require both the current QR access and an Argon2id-protected PIN for trusted
  contacts, and bind their short-lived server sessions to the trusted person;
- keep CSRF protection on state-changing browser actions;
- do not enable access logs for secret-bearing URL paths;
- erase encrypted message content after every still-authorized original
  recipient confirms reading, or at the end of the configured retention period
  after release (30 days by default and at most); and
- prefer the smallest implementation that satisfies the requirement.

When a change intentionally alters an established security or privacy
decision, document and review that decision before implementation.
