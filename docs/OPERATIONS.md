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
3. Review the notification waiting period. The default is ten minutes.
4. Enter the SMTP server details.
5. Test the SMTP connection.
6. Send a test email.
7. If the sender address is a dedicated technical mailbox, configure automatic
   delivery-failure detection as described below.
8. Open **Public information** in the administration area.
9. Add the operator's reviewed imprint, privacy information, and public contact
   address for German and English. If one version is missing, visitors see the
   configured fallback language together with a clear notice.
10. Open the public imprint, privacy, and contact pages from the footer and
   verify their content.
11. Open the public start page and create the first SilentRelay account.

SMTP is deliberately checked separately. A temporary mail-server failure does
not take the SilentRelay website offline.

### Configure the notification waiting period

Every submitted confidential message receives a fixed release time. The
default waiting period is ten minutes. Until that time, the authenticated
trusted person sees the queued message through the same personal access and
can cancel it. Cancellation erases the encrypted message content and prevents
all deliveries that have not started.

The technical administrator can configure a global value from `0` to `1440`
minutes under **Waiting period and cancellation** in the system settings. A
change affects only messages submitted afterwards; every already queued
message keeps its original release time. A value of `0` releases new messages
immediately and therefore provides no guaranteed cancellation window.

The scheduler processes a released message on its next cycle. Actual delivery
can therefore begin up to one configured scheduler interval after the displayed
release time. Once a delivery has been claimed for processing, SilentRelay no
longer promises that the message can be cancelled.

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

Open **System settings** in the administration area and configure SMTP under
**Email delivery** first.
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
- five trusted-contact PIN setup or sign-in attempts per client in 15 minutes,
  with an additional limit per QR access;
- ten complete two-step notification attempts per client and QR access in one
  hour; and
- 60 other state-changing requests per client per minute.

## Trusted-contact PIN setup

Every trusted contact must protect their QR access with a personal six-digit
PIN before submitting a message. The PIN is stored only as an Argon2id hash and
is never displayed or sent by email. Obvious PINs are rejected. Repeated wrong
entries trigger both the persistent request limits described above and a
progressive per-access lock.

New QR access details can be set up for 14 days. Access details that existed
when migration `0008` is applied receive the same one-off 14-day setup period,
starting when the migration runs. During this period they can only be used to
set a PIN; sending without a PIN is no longer possible. If setup is not
completed in time, the access expires and account management shows that new
access details are required.

The scheduler emails the account holder after successful PIN setup and after an
unused setup period expires. These emails contain neither the PIN nor secret
access details. Make sure exactly one scheduler is running and that the account
holder has at least one working, verified email address.

There is no PIN reset. A trusted contact who has forgotten their PIN contacts
the person who gave them the QR code. The account holder then selects
**Create new QR access** for the relevant trusted contact. This immediately
invalidates the old QR code, PIN and every associated trusted-contact session.
The new QR code starts a fresh 14-day setup period.

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

SilentRelay creates encrypted, data-only backups. They contain the application
database volume and the matching `.env`; they do not contain source code,
Docker images, logs, Caddy certificates, or the private age identity file.
Install `age` once on Debian:

```sh
apt-get update
apt-get install -y age
```

Run the first backup interactively:

```sh
cd /opt/silent-relay
sh backup.sh
```

`age` uses two related but different values:

| Value | Purpose | Storage |
| --- | --- | --- |
| Public age recipient, beginning with `age1` | Encrypts backups | `.backup.conf` |
| Private age identity file | Decrypts and restores backups | Separate offline location |

The guide accepts an existing public age recipient or creates a new age key
pair and tests it. When it creates a key pair, immediately copy the private age
identity file to a separate offline location. The private identity is never
stored in `.backup.conf` or in a backup. Losing it makes every backup encrypted
for its corresponding public recipient unrecoverable.

The generated `.backup.conf` stores only the public age recipient, backup
directory, installation identifier, and retention count. The public recipient
can encrypt but cannot decrypt a backup. Both manual and scheduled runs use
this file. By default, only the seven newest successful backups are retained.
Change `KEEP_BACKUPS` deliberately if a different count is required. Cleanup
affects only completed archives belonging to this installation and happens
only after a new backup succeeds.

Web and scheduler are stopped briefly so SQLite and its WAL files are
consistent. Services that were not running before the backup are not started
afterwards. The unencrypted archive is streamed directly from the maintenance
container into `age`; it is not stored on the host.

For a daily backup at 03:15, add a root cron entry:

```cron
15 3 * * * cd /opt/silent-relay && sh backup.sh >>/var/log/silentrelay-backup.log 2>&1
```

If off-site transfer is configured below, replace this backup-only entry with
the combined backup-and-transfer entry instead of adding a second backup job.

Cron never opens the setup dialog. A missing or invalid configuration produces
a non-zero exit status. Monitor that status or the log. A backup kept only on
the SilentRelay server does not protect against loss of that server. Copy the
encrypted archive to a separate, access-controlled system.

### Transfer backups off site

`backup-transfer.sh` is independent of archive creation. It transfers only a
completed `age`-encrypted archive and never uploads `.env`, the database, or
the private age identity separately. Copy `.backup-transfer.conf.example` to
`.backup-transfer.conf` and configure either SFTP or HTTPS WebDAV.

#### Configure SFTP step by step

The following example uses a separate Debian backup server,
`backup.example.org`, with SSH on port 22. Replace host names, addresses, and
paths with the actual values. It assumes that SSH is already operational and
reachable from the SilentRelay server. If a firewall restricts SSH, configure
it according to the backup server's existing operating policy.

1. **On the backup server**, create a dedicated account and directory. The
   account has no interactive shell:

   ```sh
   adduser --system --group --home /var/backups/silent-relay \
     --shell /usr/sbin/nologin silent-relay
   chmod 700 /var/backups/silent-relay
   chown silent-relay:silent-relay /var/backups/silent-relay
   ```

2. **On the backup server**, store authorized keys outside the SFTP-writable
   directory. Open the dedicated configuration:

   ```sh
   nano /etc/ssh/sshd_config.d/silent-relay-backup.conf
   ```

   Enter:

   ```text
   Match User silent-relay
       AuthorizedKeysFile /etc/ssh/authorized_keys/%u
       AuthenticationMethods publickey
       PasswordAuthentication no
       KbdInteractiveAuthentication no
       ForceCommand internal-sftp -d /var/backups/silent-relay
       DisableForwarding yes
       PermitTTY no
   ```

   Prepare the root-managed key location, validate the complete SSH
   configuration, and reload it only after validation succeeds:

   ```sh
   install -d -m 755 -o root -g root /etc/ssh/authorized_keys
   touch /etc/ssh/authorized_keys/silent-relay
   chown root:root /etc/ssh/authorized_keys/silent-relay
   chmod 644 /etc/ssh/authorized_keys/silent-relay
   sshd -t
   systemctl reload ssh
   ```

   `sshd -t` must produce no output. Replace `SOURCE_IP` below with the fixed
   public address of the SilentRelay server and verify the effective settings:

   ```sh
   sshd -T -C user=silent-relay,host=backup.example.org,addr=SOURCE_IP | \
     grep -E 'authorizedkeysfile|authenticationmethods|passwordauthentication|kbdinteractiveauthentication|forcecommand|disableforwarding|permittty'
   ```

   The output must show the configured external authorized-key file,
   public-key-only authentication, `internal-sftp`, disabled forwarding, and no
   TTY.

   Mode `0644` is intentional for this root-owned external public-key file:
   the authentication process must be able to read it, while only root can
   change it. A public key is not a secret.

   `ForceCommand internal-sftp -d /var/backups/silent-relay` selects the
   initial directory but is not a chroot. Normal Unix permissions remain the
   boundary for other filesystem paths.

3. **On the SilentRelay server**, create a dedicated client key. It deliberately
   has no passphrase because the restricted account must work unattended from
   cron. Protect the private key with mode `0600` and never copy it to the
   backup server:

   ```sh
   install -d -m 700 /root/.ssh
   if [ -e /root/.ssh/silentrelay-backup ] || \
      [ -e /root/.ssh/silentrelay-backup.pub ]; then
       printf '%s\n' 'Backup transfer key already exists; stop and inspect it.' >&2
   else
       ssh-keygen -t ed25519 -f /root/.ssh/silentrelay-backup -N '' \
         -C 'SilentRelay off-site backup'
       chmod 600 /root/.ssh/silentrelay-backup
       chmod 644 /root/.ssh/silentrelay-backup.pub
   fi
   ```

   If the warning is printed, do not generate or overwrite anything until the
   existing files have been identified.

4. **From the SilentRelay server to the backup server**, copy the single line from
   `/root/.ssh/silentrelay-backup.pub` to the backup server's
   `/etc/ssh/authorized_keys/silent-relay`. Prefix it with `restrict` for
   defense in depth:

   ```sh
   cat /root/.ssh/silentrelay-backup.pub
   ```

   **On the backup server**, open the destination with:

   ```sh
   nano /etc/ssh/authorized_keys/silent-relay
   ```

   ```text
   restrict ssh-ed25519 AAAA... SilentRelay off-site backup
   ```

   Then restore the intended ownership and mode on the backup server:

   ```sh
   chown root:root /etc/ssh/authorized_keys/silent-relay
   chmod 644 /etc/ssh/authorized_keys/silent-relay
   ssh-keygen -lf /etc/ssh/authorized_keys/silent-relay
   ```

5. **On the backup server**, obtain the Ed25519 host-key fingerprint through the
   already trusted administrative connection:

   ```sh
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

   **On the SilentRelay server**, scan into a temporary file and compare every
   displayed fingerprint with that independently obtained value. Never skip
   this comparison and never set `StrictHostKeyChecking=no`:

   ```sh
   scan_file=$(mktemp)
   ssh-keyscan -T 10 -p 22 -t ed25519 backup.example.org > "$scan_file"
   if [ ! -s "$scan_file" ]; then
       printf '%s\n' 'No host key was received; stop and check SSH reachability.' >&2
       rm -f "$scan_file"
   else
       grep -v '^[[:space:]]*#' "$scan_file" | \
         cut -d ' ' -f 2- | ssh-keygen -lf -
   fi
   ```

   Every displayed fingerprint must exactly match the value obtained through
   the trusted administrative connection. An empty scan or any mismatch is a
   hard stop. Only after an exact match, install the pinned host-key file:

   ```sh
   install -m 600 "$scan_file" /root/.ssh/silentrelay-backup-known-hosts
   rm -f "$scan_file"
   ```

6. **On the SilentRelay server**, test the restricted account. It must not ask
   for a password and must show the configured backup directory:

   ```sh
   printf 'pwd\nls -la\nquit\n' | sftp -q -b - \
     -i /root/.ssh/silentrelay-backup \
     -o BatchMode=yes \
     -o IdentitiesOnly=yes \
     -o StrictHostKeyChecking=yes \
     -o UserKnownHostsFile=/root/.ssh/silentrelay-backup-known-hosts \
     -P 22 silent-relay@backup.example.org
   ```

7. **On the SilentRelay server**, copy and edit the example configuration:

   ```sh
   cd /opt/silent-relay
   [ -e .backup-transfer.conf ] || \
     cp .backup-transfer.conf.example .backup-transfer.conf
   nano .backup-transfer.conf
   ```

   Keep the SFTP entries below and replace their values. Commented example
   lines may remain in the file:

   ```text
   TRANSFER_TARGET=sftp
   SFTP_HOST=backup.example.org
   SFTP_PORT=22
   SFTP_USER=silent-relay
   SFTP_DIRECTORY=/var/backups/silent-relay
   SFTP_IDENTITY_FILE=/root/.ssh/silentrelay-backup
   SFTP_KNOWN_HOSTS_FILE=/root/.ssh/silentrelay-backup-known-hosts
   ```

   ```sh
   chmod 600 /opt/silent-relay/.backup-transfer.conf
   ```

The SFTP configuration is now ready for the common transfer test and cron setup
below.

If the SFTP test only reports `Connection closed`, repeat it without `-q` and
with `-vv`, then inspect `journalctl -u ssh` on the backup server. A message
that the external authorized-keys file cannot be opened normally means that a
root-owned file was incorrectly set to mode `0600`; restore mode `0644`. If
`ssh-keyscan` creates an empty file, verify SSH reachability from the
SilentRelay server and any existing network policy.
Comment lines emitted by `ssh-keyscan` are valid in a known-hosts file and do
not represent additional keys.

#### Configure WebDAV

The following example uses Nextcloud at `cloud.example.org`, a dedicated
`silent-relay` account with an app password, and the WebDAV directory
`/backups`. Replace these values with the actual installation. HTTPS is
mandatory. The target directory must already exist and be writable by the
dedicated account. Create an app password in that Nextcloud account; do not use
the account password for backup transfer.

1. **On the SilentRelay server**, create a private configuration directory and
   open the dedicated curl netrc file:

   ```sh
   install -d -m 700 /root/.config/silent-relay
   nano /root/.config/silent-relay/backup-webdav.netrc
   ```

   Enter exactly one line with the dedicated account and app password:

   ```text
   machine cloud.example.org login silent-relay password replace-with-app-password
   ```

   Protect the credential file and do not display its contents in logs or
   support output:

   ```sh
   chmod 600 /root/.config/silent-relay/backup-webdav.netrc
   ls -l /root/.config/silent-relay/backup-webdav.netrc
   ```

   The mode must be `-rw-------`.

2. **On the SilentRelay server**, copy and edit the example transfer
   configuration:

   ```sh
   cd /opt/silent-relay
   [ -e .backup-transfer.conf ] || \
     cp .backup-transfer.conf.example .backup-transfer.conf
   nano .backup-transfer.conf
   ```

   Keep exactly these active entries:

   ```text
   TRANSFER_TARGET=webdav
   WEBDAV_BASE_URL=https://cloud.example.org/remote.php/dav/files/silent-relay/backups
   WEBDAV_CREDENTIAL_FILE=/root/.config/silent-relay/backup-webdav.netrc
   ```

   Comment out or remove active SFTP entries. The strict parser rejects options
   belonging to a different target, while commented example lines may remain.
   Then protect the configuration:

   ```sh
   chmod 600 /opt/silent-relay/.backup-transfer.conf
   ```

The WebDAV configuration is now ready for the common transfer test and cron
setup below.

Do not put a password in `.backup-transfer.conf` or in the WebDAV URL.

#### Test and schedule off-site transfer

The following steps apply to both SFTP and WebDAV. **On the SilentRelay
server**, test a manual transfer first. Without an argument the script selects
the newest completed backup for this installation; an explicit archive is also
accepted:

```sh
cd /opt/silent-relay
sh backup-transfer.sh
sh backup-transfer.sh /var/backups/silent-relay/silentrelay-....tar.gz.age
```

Success prints `Backup transferred: <archive name>`. On the backup target,
confirm that the completed `.tar.gz.age` file exists with the expected size and
that no `.partial` file remains. For an SFTP target:

```sh
ls -lah /var/backups/silent-relay
find /var/backups/silent-relay -maxdepth 1 -name '*.partial' -print
```

The `find` command must produce no output after success.

For WebDAV, confirm that the completed encrypted archive is visible in the
target account.

The script uploads to a unique `.partial` name, compares the remote and local
sizes, and only then renames or moves it to the final name without requesting
overwrite. A failed transfer returns a non-zero status, attempts to remove its
partial upload, and always preserves the local archive. It never deletes
completed remote backups. Configure retention, snapshots, or immutable storage
on the target independently.

**On the SilentRelay server**, edit root's crontab:

```sh
crontab -e
```

Replace an existing backup-only entry instead of adding a second backup job.
Run creation and transfer as separate stages in one monitored cron job:

```cron
15 3 * * * cd /opt/silent-relay && { sh backup.sh && sh backup-transfer.sh; } >>/var/log/silentrelay-backup.log 2>&1
```

Because of `&&`, transfer starts only after successful archive creation. A
transfer failure makes the cron command fail while leaving the new local
backup available for a retry. The braces ensure that output from both scripts
reaches the same log. Verify the installed entry and run the exact grouped
command manually once:

```sh
crontab -l
cd /opt/silent-relay && { sh backup.sh && sh backup-transfer.sh; } >>/var/log/silentrelay-backup.log 2>&1
tail -n 100 /var/log/silentrelay-backup.log
```

The log must contain both `Backup created:` and `Backup transferred:`. Monitor
the cron exit status or log and periodically perform a full restore test using
the separately stored private age identity.

A compromised SilentRelay server can still alter or delete every remote backup
reachable with its credentials. Prefer target-side snapshots, append-only
permissions, or an external system that pulls backups when stronger protection
is required.

To rotate an SFTP client key safely, create the replacement under a new
filename, add its public key alongside the existing key, test SFTP with the new
private key, update `.backup-transfer.conf`, and complete one real transfer.
Only then remove the old authorized key and private key.

Do not consider a backup complete until its restoration has been tested. Avoid
copying individual files from a running Docker volume.

## Restore SilentRelay

Restore is intentionally limited to a fresh installation. It refuses an
existing `.env` or non-empty application data volume, so it cannot silently
replace a running installation. Obtain the software from the repository, then
run the guided restore with the encrypted archive and separately stored private
age identity file:

```sh
git clone https://github.com/bndtblds/silent-relay.git /opt/silent-relay
cd /opt/silent-relay
apt-get install -y age
sh restore.sh /path/to/silentrelay-....tar.gz.age /secure/backup-age-identity.key
```

The second argument is the private age identity file, not the public `age1...`
recipient from `.backup.conf`. The tool requires the word `RESTORE`,
authenticates and validates the complete
archive in an isolated container area, verifies every checksum and path, and
enforces safe default limits of 2 GiB per file, 10 GiB in total, and 10,000
files. It also verifies the declared file sizes and available storage. Only
then does it write `.env` and the application volume. It runs the database
migrations and starts the deployment afterwards. Caddy obtains fresh
certificates on the target server.

Larger installations require an explicit limit override for that restore. Set
one or more variables only after checking the expected backup size and free
space, for example:

```sh
SILENTRELAY_RESTORE_MAX_FILE_BYTES=4294967296 \
SILENTRELAY_RESTORE_MAX_TOTAL_BYTES=21474836480 \
SILENTRELAY_RESTORE_MAX_FILE_COUNT=20000 \
sh restore.sh /path/to/silentrelay-....tar.gz.age /secure/backup-age-identity.key
```

The values are byte and file-count limits. Raising them reduces protection
against an unexpectedly large or maliciously substituted encrypted archive.

For a server migration, create one final manual backup after stopping use of
the old installation. Transfer the encrypted archive and private age identity
file through separate protected paths, restore on the new server, verify
`/health/ready`, sign in, and send a test notification. Change DNS only after
those checks pass. Do not run both installations concurrently against the same
technical mailbox.

To recover an existing server deliberately, use `--replace`:

```sh
cd /opt/silent-relay
sh restore.sh --replace /path/to/silentrelay-....tar.gz.age /secure/backup-age-identity.key
```

This path requires the exact word `REPLACE`. Before changing current data it
creates a mandatory new encrypted safety backup with the public age recipient
configured in `.backup.conf`.
It then stops web and scheduler, validates the selected backup, replaces only
`.env` and `silentrelay-data`, migrates the database, and starts the deployment.
If the safety backup fails, replacement does not begin.

Replacement is not atomic. After complete validation, SilentRelay removes the
current files and copies the staged files into place. A host or storage failure
during this interval can leave an incomplete installation. Correct this by
running the selected restore again. If that archive is no longer usable,
restore the mandatory safety backup created immediately beforehand.

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

Changing the account password or replacing the account-owner access link
immediately signs out every active account-owner browser session for that
account. Sign in again with the new password or new access link.

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
