# SilentRelay

SilentRelay is a self-hosted notification service for small, trusted groups.
It lets a trusted person send a confidential message without seeing who will
receive it or which contact details are stored.

The account owner decides who belongs to the group, which contact methods are
used, and which trusted persons may send messages. SilentRelay then determines
the recipients on the server and delivers the message by email.

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
- Partners and trusted persons within one private group
- Secret, printable QR access for each trusted person
- Confidential message submission with a review step
- Server-side recipient selection without exposing names or recipient counts
- Verified email contact methods
- A separate technical administration area
- Encrypted sensitive data and automatic removal of delivered message content

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
   and technical administrator credentials. It creates all secrets, starts
   SilentRelay, initializes the database, and checks the application.

4. Open `https://your-domain.example/admin/login`, sign in, and configure SMTP
   under system settings.
5. Open the public start page and create the first account.

Caddy obtains and renews HTTPS certificates automatically. Additional domains
are redirected to the primary domain, which SilentRelay uses for all generated
links and QR codes. Every entered domain must point to the server before setup.
The setup never stores the administrator password in clear text and never
overwrites an existing `.env` file. Keep `.env` separate from database backups.

Manual configuration and advanced deployment options are documented in
[Operations](docs/OPERATIONS.md).

## Day-to-day operation

Run one `web` service and exactly one `scheduler` when using the default SQLite
database. The scheduler sends queued notifications and performs recurring
cleanup.

Useful commands:

```sh
docker compose ps
docker compose logs caddy web scheduler
docker compose restart caddy web scheduler
```

Before an update, create a verified backup of both the database volume and the
secrets. Then build or pull the new version, run the migration service, and
restart the application. See [Operations](docs/OPERATIONS.md) for backup,
restore, update, health-check, and security guidance.

## Documentation

- [Operations](docs/OPERATIONS.md) — configuration, deployment, backup, restore,
  updates, and maintenance
- [Development](docs/DEVELOPMENT.md) — local setup and tests
- [Threat model](docs/THREAT_MODEL.md) — security boundaries and risks

## License

See [LICENSE](LICENSE).
