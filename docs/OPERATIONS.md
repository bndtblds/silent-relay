# Operating SilentRelay

This guide is for people who run their own SilentRelay server. It covers the
recommended installation, routine checks, updates, and the current backup
options. Development details are documented separately in
[DEVELOPMENT.md](DEVELOPMENT.md).

Self-hosted installations use the built-in `allow_all` entitlement provider
and require no external service. Before deliberately installing an alternative
registration provider, review [ENTITLEMENTS.md](ENTITLEMENTS.md), including its
fail-closed behavior.

## Before you begin

You need:

- a server with Debian 13 or another Docker-supported operating system;
- a public domain that points to the server;
- inbound ports `80` and `443` open in the server firewall;
- an SMTP account for sending email; and
- administrator access to the server.

The examples below use `/opt/silent-relay` as the installation directory and
are run as `root`. If you use another administrator account, prefix system
commands with `sudo`.

## Install Docker on Debian 13

If Docker Engine and the Docker Compose plugin are already installed, continue
with [Install SilentRelay](#install-silentrelay).

Install the required packages and Docker signing key:

```sh
apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
```

Add Docker's package source:

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

Install and start Docker:

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

The last three commands must complete successfully. For another operating
system, follow Docker's
[official installation guide](https://docs.docker.com/engine/install/).

## Install SilentRelay

Before continuing, make sure every domain that you want to use points to this
server. Open inbound TCP ports `80` and `443`. UDP port `443` is optional and
enables HTTP/3.

Download SilentRelay:

```sh
git clone https://github.com/bndtblds/silent-relay.git /opt/silent-relay
cd /opt/silent-relay
```

Start the guided setup:

```sh
sh setup.sh
```

The setup asks for:

- the primary public domain;
- optional additional domains;
- the technical administrator's username; and
- the technical administrator's password.

English is the default fallback language. To use German when a browser does not
request a supported language, set `DEFAULT_LANGUAGE=de` in `.env`. Supported
values are `de` and `en`.

It then creates the private configuration, starts SilentRelay, initializes the
database, and checks whether the application is ready. It does not store the
administrator password in clear text.

The setup stops without changing anything if `/opt/silent-relay/.env` already
exists. Do not delete this file to repeat the setup: it contains the keys
required to read the existing encrypted data.

When setup finishes, open:

```text
https://your-primary-domain.example/admin/login
```

Replace the example address with the primary domain entered during setup.
Caddy obtains and renews the HTTPS certificate automatically. Additional
domains redirect to the primary domain.

## Complete the first setup

1. Sign in to `/admin/login`.
2. Open the system settings.
3. Enter the SMTP server details.
4. Test the SMTP connection.
5. Send a test email.
6. If the sender address is a dedicated technical mailbox, configure automatic
   delivery-failure detection as described below.
7. Open **Public information** in the administration area.
8. Add the operator's reviewed imprint, privacy information, and public contact
   address for German and English. If one version is missing, visitors see the
   configured fallback language together with a clear notice.
9. Open the public imprint, privacy, and contact pages from the footer and
   verify their content.
10. Open the public start page and create the first SilentRelay account.

SMTP is deliberately checked separately. A temporary mail-server failure does
not take the SilentRelay website offline.

### Detect permanent email delivery failures

This optional function requires the SMTP sender address to be its own technical
mailbox. The mailbox must support:

- plus addressing, for example
  `notifications+random-code@example.org`; and
- encrypted IMAP access, normally on port `993`.

Because SilentRelay appends a secure correlation code, the part of the sender
address before `@` may contain at most 20 bytes. A short address such as
`notifications@example.org` is suitable.

Mailcow supports these features, but Mailcow is not required. Ask the provider
of the existing mailbox whether plus addressing and IMAP are available.

Open **Email delivery** in the administration area and configure SMTP first.
The sender address should be the address of this technical mailbox. In
**Detect delivery failures automatically**, enter its IMAP server, port,
username, and password. To acknowledge the deletion rule, type the exact sender
address shown by SilentRelay.

Once activated, SilentRelay fully manages this mailbox and permanently deletes
every incoming message after inspection. This includes processed delivery
reports, ordinary replies, spam, and malformed messages. Never use the mailbox
for support, personal correspondence, or another purpose.

Use **Test IMAP without deleting mail** after saving. This test opens only the
configured inbox in read-only mode. It does not read or delete messages.
Thereafter the scheduler regularly processes and empties that inbox.

A permanent, correlated delivery report makes the affected contact method
unconfirmed, so the account owner sees a plain-language warning and must verify
the address again. A delayed report does not trigger another application send
while the receiving mail server is still retrying. No delivery report is not
proof that a message arrived, so regular account and contact confirmation
remains necessary.

Some mail servers reject an unknown recipient immediately during the SMTP
conversation instead of accepting the message and returning a later report.
SilentRelay handles this direct permanent rejection in the same way: it marks
the affected contact method unconfirmed and undeliverable, stops retrying an
affected notification, and explains the problem to the account owner. No
message appears in the technical inbox in this case because the receiving
server never accepted it.

All SilentRelay emails state that they are automatic and that replies are not
read and are deleted. Disable incoming processing before using the mailbox
manually, and remember that messages already deleted by SilentRelay cannot be
recovered.

### Regular contact confirmation

SilentRelay uses the account-review interval for every confirmed email
address belonging to the account owner or a partner:

1. Before the due date, the account owner receives a plain reminder. It never
   contains the secret account-owner access link.
2. On the due date, SilentRelay automatically sends a separate one-time
   confirmation link to every active, confirmed address.
3. Addresses remain usable during the configured review grace period.
4. An address that is not confirmed before the deadline becomes unconfirmed
   and no longer receives confidential notifications.
5. The account owner must also confirm in account management that people,
   trusted persons, and assignments remain current.

Opening a confirmation link only shows a neutral page. The address is not
confirmed until the person deliberately selects the confirmation button.
Automatic link checks by mail-security systems therefore do not complete the
confirmation.

The account review finishes only when both the people and every remaining
contact method have been confirmed. Permanent SMTP rejection and final
delivery reports still invalidate an address immediately.

As long as an address is undeliverable or its regular confirmation has
expired, SilentRelay reminds every other confirmed account-owner address.
`CONTACT_PROBLEM_REMINDER_DAYS` in `.env` controls the interval and defaults
to seven days. The account owner should therefore add at least two personal
addresses. With only one address, SilentRelay works but cannot notify the
account owner if that address fails.

SilentRelay safely displays the operator text but does not generate universal
legal wording. The operator remains responsible for the completeness and
accuracy of the published information.

The text fields preserve entered line breaks and support a small, safe subset
of Markdown for headings, separators, lists, bold or italic text, and links.
The formatting help next to the fields shows short examples. Links may use
`https`, `http`, or `mailto`. HTML, images, and embedded content are
deliberately not displayed. Always check all three public pages after saving.

Public pages and the technical administration use the current browser
language. Account owners select one language when creating an account. That
choice then applies consistently to account pages, trusted-person pages, and
emails, regardless of later browser settings.

## Understand abuse protection

SilentRelay stores rate-limit counters in SQLite. They survive web restarts and
are shared by every web process that uses the same database. The database never
stores a plain client IP address, secret URL or QR access for this purpose.
Client identifiers and secret-access scopes are stored only as keyed hashes;
IPv6 client addresses are grouped by `/64`. Expired counters are removed during
protected requests and by the scheduler.

Only `POST` requests consume a limit. The default limits are:

- five technical-admin or account sign-in attempts per client in 15 minutes;
- three account-creation attempts per client in one hour;
- ten contact-confirmation attempts per client and secret access in one hour;
- ten complete two-step notification attempts per client and QR access in one
  hour; and
- 60 other state-changing requests per client per minute.

Installation-wide safety limits supplement the per-client and per-access
limits. The bucket table is capped at 50,000 current entries and fails closed
when no new entry can be stored. Every rejected request returns HTTP `429` with
a `Retry-After` header. If the rate-limit database cannot be used, protected
requests fail closed with HTTP `503`.

The values can be changed with the `RATE_LIMIT_*` settings shown in
`.env.example`. Restart `web` after changing them. Increasing a limit should be
a deliberate operational decision, not a workaround for unexplained traffic.
Rate limiting reduces automated abuse but does not replace firewalling,
capacity limits or upstream denial-of-service protection.

In the provided Compose deployment, Caddy is the only public service and
Uvicorn accepts forwarded client addresses only on the internal application
network. Do not expose the `web` container directly while trusting arbitrary
forwarding headers.

## Check the running system

Change to the installation directory before running Docker commands:

```sh
cd /opt/silent-relay
docker compose ps
```

`caddy`, `web`, and `scheduler` must be running. `web` should also be shown as
healthy. The `migrate` service normally appears as completed because it exits
after checking the database.

Show the most recent application logs:

```sh
docker compose logs --tail=100 caddy web scheduler
```

Check the website from the server:

```sh
curl --fail --silent --show-error \
  https://your-primary-domain.example/health/ready
```

Replace the example domain. A successful request confirms that the application
configuration and database are available.

SilentRelay intentionally does not write access logs containing secret account,
verification, or notification links.

## Restart SilentRelay

Use this after a server restart or when the running services need to be
restarted:

```sh
cd /opt/silent-relay
docker compose restart caddy web scheduler
docker compose ps
```

Do not start a second `scheduler`. The standard Compose configuration already
runs exactly one.

Delivery processing uses a two-minute database lease. The normal SMTP
connection timeout is 15 seconds, so the lease is long enough for an ordinary
attempt while still allowing prompt recovery. If the scheduler process or
container stops after claiming a delivery, restart it normally. A later cycle
may reclaim the delivery after the lease expires; no manual database change is
required. Existing `processing` deliveries from an older version become
immediately reclaimable during the database migration.

SQLite deployments support exactly one scheduler instance. The lease protects
against an accidentally overlapping cycle or a restart, but it is not a basis
for operating multiple scheduler containers. Because SMTP acceptance and the
following SQLite commit cannot be atomic, a crash after SMTP accepts a message
but before SilentRelay commits `delivered` may cause that external email to be
sent again after recovery. SilentRelay therefore does not promise exactly-once
email delivery.

## Update SilentRelay

Create a current backup before every update. Then inspect the installation:

```sh
cd /opt/silent-relay
git status --short
```

The second command must produce no output. Stop if it lists unexpected files
and clarify why they were changed. Do not delete `.env` and do not discard
unknown changes merely to continue.

Download the new version:

```sh
git pull --ff-only
```

Build and start it:

```sh
docker compose up -d --build
```

This command checks the database before starting the updated application. If it
fails, do not force the other services to start. Read the error and the
migration log:

```sh
docker compose logs migrate
```

After a successful update, verify the installed commit and services:

```sh
git rev-parse --short HEAD
docker compose ps
docker compose logs --tail=100 caddy web scheduler
```

Then open the public website and test the area affected by the update.

The update keeps:

- `/opt/silent-relay/.env`;
- all accounts and application data;
- SMTP settings; and
- existing HTTPS certificates.

Never use `docker compose down --volumes` during an update. The `--volumes`
option deletes persistent data.

## Back up SilentRelay

SilentRelay does not yet include a guided backup and restore script. Until one
is available, the simplest complete backup is an encrypted snapshot or backup
of the entire server.

For a consistent snapshot, stop SilentRelay first:

```sh
cd /opt/silent-relay
docker compose stop
```

Create the server snapshot or backup with the tools supplied by the hosting
provider. Afterwards, start SilentRelay again:

```sh
docker compose up -d
docker compose ps
```

Also keep an encrypted, access-controlled backup of
`/opt/silent-relay/.env` in a separate safe location. This file contains the
keys needed to read encrypted database fields. A database without its matching
`.env` file is not a usable backup.

Do not consider a backup complete until its restoration has been tested. Avoid
copying individual files out of a running Docker data volume: the resulting
SQLite copy may be inconsistent.

## Restore SilentRelay

Restoring a complete server snapshot is currently the recommended method.
Follow the hosting provider's restore procedure, then verify:

```sh
cd /opt/silent-relay
docker compose up -d
docker compose ps
docker compose logs --tail=100 caddy web scheduler
```

Open the public website, check `/health/ready`, sign in, and send a test
notification through a dedicated test account.

Moving individual Docker volumes to a new server is an advanced manual
procedure. SilentRelay does not yet provide a guided restore tool for it.

## If the website does not open

Check the following in this order:

1. Does the domain point to the server's public IP address?
2. Are inbound TCP ports `80` and `443` open?
3. What does `docker compose ps` report?
4. What do the recent logs report?

```sh
cd /opt/silent-relay
docker compose ps
docker compose logs --tail=100 caddy web scheduler
```

If Caddy cannot obtain an HTTPS certificate, check the domain and firewall
first. If `web` is unhealthy, inspect its log. If email fails while the website
works, use the SMTP connection and test-email actions in the administration
area.

## Lost access

SilentRelay version 1 does not provide password or account-owner access
recovery. Keep the printed account-owner access and its password securely and
separately.

The field-encryption key cannot currently be rotated through the administration
area or a supplied maintenance command. Do not replace keys in `.env` on an
existing installation: doing so makes encrypted data unreadable. Restore the
matching `.env` and database from backup if keys are lost.

## Production checklist

- The primary domain opens over HTTPS.
- Only ports `80` and `443` are publicly exposed for SilentRelay.
- The `.env` file and backups are protected.
- `caddy`, `web`, and exactly one `scheduler` are running.
- `web` is healthy.
- SMTP connection and test-email delivery succeed.
- Imprint, privacy information, and contact details are complete and publicly
  reachable from the footer.
- A test account can complete the notification flow.
- Backup restoration has been tested.
