# SilentRelay

SilentRelay is a self-hosted notification service for small, trusted groups.
It lets a trusted person send a confidential message without seeing who will
receive it or which contact details are stored.

The account owner decides who belongs to the group, which contact methods are
used, and which trusted persons may send messages. SilentRelay fixes the
account holder and every active partner with completed personal-access setup as
recipients on the server at release time. The confidential message is available
in each recipient's authenticated SilentRelay inbox even without a verified
contact method; active verified contact methods receive only neutral email
notices.

SilentRelay is not an emergency service, medical alarm, messenger, or
high-availability system.

## Who it is for

Imagine a couple whose relationship is not legally recognized. If something
happens to one partner, the other may not be contacted or receive any
information.

With SilentRelay, they can define their private notification group in advance.
The account owner creates the group, adds their partner, and assigns trusted
persons to themselves or to their partner. Each trusted person receives a
private QR code. If a trusted person learns that something has happened, they
can submit a confidential message. SilentRelay forwards it to the eligible
members of the predefined group without revealing their names, contact details,
or number to the sender.

SilentRelay helps bridge this communication gap while keeping personal contact
data under the operator's control. It does not create legal rights and does not
replace emergency services or official arrangements.

## What SilentRelay provides

- A guided setup for account owners
- Partners with personal secret access plus password, and trusted persons
  within one private group
- Secret, printable QR access plus a personal six-digit PIN for each trusted
  person; authenticated trusted persons can change their PIN themselves
- Confidential message submission with a review step, a ten-minute waiting
  period by default, and cancellation before release for delivery
- Server-side recipient selection without exposing names or recipient counts
- Individually verified email contact methods with automatic periodic
  reconfirmation
- Optional automatic detection of permanent email delivery failures
- German and English user interfaces with one consistent language per account
- A separate technical administration area
- Immediate browser feedback when repeated password or PIN entries do not
  match, backed by mandatory server-side validation and rejection of basic
  trivial credential patterns
- Protected personal inboxes for the account holder and activated partners
- Inbox delivery does not depend on email availability; verified contact
  methods are used only for neutral availability notices
- Encrypted names for the account holder, partners, and trusted contacts; the
  relevant account-holder or partner name is shown only in authorized inboxes
- Encrypted message storage until every still-authorized original recipient
  confirms reading, but no longer than the configured retention period after
  release (30 days by default and at most)

Email notices never contain the confidential text, the person concerned, a
relationship, recipient count, or secret access details. Partner links and QR
codes are shown once to the account holder for direct handover and are never
sent by email. SilentRelay cannot prevent screenshots, copied content, or
disclosure from a compromised authenticated browser.

In the recommended production deployment, recipients retrieve confidential
content directly from SilentRelay over authenticated HTTPS. This protects the
transport between the browser and the SilentRelay instance. Email transport
does not provide the same end-to-end guarantee: SMTP TLS can be hop-by-hop or
opportunistic, and messages may persist in receiving servers, mailboxes,
forwarding systems, clients, and their backups. HTTPS does not protect content
after display on a compromised device and cannot prevent copying or screenshots.

## Requirements

- A server with Docker Engine and Docker Compose
- Git
- A public domain pointing to the server
- Publicly reachable ports `80` and `443`
- An SMTP account for sending email

Install Docker Engine and the Docker Compose plugin using the
[official instructions for your Linux distribution](https://docs.docker.com/engine/install/).
Package names and installation steps vary between distributions.

Verify the prerequisites:

```sh
git --version
docker version
docker compose version
```

## Install with Docker

1. Create the recommended application directory, clone SilentRelay, and enter
   the repository:

   ```sh
   sudo install -d -o "$USER" -g "$USER" /opt/silent-relay
   git clone https://github.com/bndtblds/silent-relay.git /opt/silent-relay
   cd /opt/silent-relay
   ```

   When already working as `root`, omit `sudo`.

2. Make sure the domain points to the server and ports `80` and `443` are open.
3. Start the interactive setup:

   On Linux:

   ```sh
   sh setup.sh
   ```

   On Windows:

   ```powershell
   .\setup.ps1
   ```

   The setup asks for the primary public domain, optional additional domains,
   technical administrator credentials, and uses English as the initial
   fallback language. It creates all secrets, starts
   SilentRelay, initializes the database, and checks the application.

4. Open `https://your-domain.example/admin/login` and sign in.
5. Review the operational and retention settings, then configure and test SMTP
   under system settings. SMTP is stored only in the application database.
   Optionally enable automatic
   delivery-failure detection for the same dedicated technical mailbox.
6. Add the deployment's imprint, privacy information, and public contact
   address under public information.
7. Open the public start page and create the first account.

SilentRelay currently supports German and English. Public pages and the
technical administration follow the browser language. The account owner
chooses the account language during creation and can change it later for all
account pages, QR-code handovers, notifications, and emails.

Caddy obtains and renews HTTPS certificates automatically. Additional domains
are redirected to the primary domain, which SilentRelay uses for all generated
links and QR codes. Every entered domain must point to the server before setup.
The setup never stores the administrator password in clear text and never
overwrites an existing `.env` file. Never place `.env` beside an ordinary
database copy in plaintext. SilentRelay's guided backup deliberately includes
the matching `.env` and database together inside one authenticated,
`age`-encrypted archive because both are required for a usable restore.

Manual configuration and advanced deployment options are documented in
[Operations](docs/OPERATIONS.md).

## Day-to-day operation

Run one `web` service and exactly one `scheduler` when using the default SQLite
database. The scheduler sends queued notifications and performs recurring
cleanup. Newly submitted confidential messages remain queued for ten minutes
by default. During that time, the trusted person sees that the message has not
yet been sent and can cancel it through the same personal access. The technical
administrator can configure a waiting period from 0 to 1,440 minutes; changes
apply only to messages submitted afterwards.

Useful commands:

```sh
docker compose ps
docker compose logs caddy web scheduler
docker compose restart caddy web scheduler
```

Run `sh update.sh` for an update. It refuses a dirty worktree and first checks
the tracked upstream branch. If no new commit is available, it exits without
stopping or rebuilding services. Otherwise it requires a successful encrypted
backup and off-site transfer, updates only by fast-forward, rebuilds the
deployment, runs migrations, and verifies readiness. `backup.sh`
creates the encrypted backup of the database volume together with the matching
private application configuration. `backup-transfer.sh` transfers it to a
separate SFTP or HTTPS WebDAV system. `restore.sh`
restores such a backup into a fresh
installation or a new server. See [Operations](docs/OPERATIONS.md) for key
setup, the distinction between the public encryption recipient and private
decryption identity, off-server storage, restore testing, migration, update,
and security guidance.

## Documentation

- [Operations](docs/OPERATIONS.md) — configuration, deployment, backup, restore,
  updates, and maintenance
- [Development](docs/DEVELOPMENT.md) — local setup and tests
- [Threat model](docs/THREAT_MODEL.md) — security boundaries and risks

## License

See [LICENSE](LICENSE).
