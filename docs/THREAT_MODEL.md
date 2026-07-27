# SilentRelay security and threat model

This document explains what SilentRelay protects, which systems must be
trusted, and which risks remain. It is intended for operators and contributors
who understand the basic deployment but are not security specialists.

## What SilentRelay protects

SilentRelay handles information that should remain private:

- names of partners and trusted persons;
- email contact methods;
- confidential notification text;
- account-owner and trusted-person access links;
- passwords, sessions, and technical mail credentials; and
- the private structure of each notification group.

The main goal is to prevent accidental disclosure through the website,
database, logs, browser behavior, or trusted-person interface.

A trusted person may submit a message but must not learn who receives it, how
many people receive it, or how the account is organized.

## What SilentRelay cannot protect against

SilentRelay cannot keep data secret from a fully compromised server. An
attacker who controls the host can read application memory, runtime keys, and
decrypted data.

Email is not end-to-end encrypted by SilentRelay. The configured SMTP server
and the recipients' mail systems necessarily receive the message text.

SilentRelay is not an emergency, medical, or high-availability service. A
server, network, DNS, or mail outage can delay or prevent delivery.

## Systems and people that must be trusted

The following components form the trusted operating environment:

- the server and operating system;
- Docker and the running SilentRelay containers;
- Caddy and the internal application network;
- the `.env` file and its cryptographic keys;
- the persistent database volume;
- the configured SMTP and IMAP service; and
- the technical administrator.

The account owner is trusted to configure the correct people and contact
methods. Trusted persons are authorized to submit messages, but they are not
trusted with account or recipient information.

## Main risks and protections

| Risk | SilentRelay protection |
|---|---|
| Someone photographs a trusted-person QR code | The access can be deactivated or replaced. Requests are rate-limited and reveal no account or recipient details. |
| Someone obtains the account-owner QR code | The management area additionally requires the account password and creates a short-lived server session. |
| Secret links appear in logs or referrer headers | Proxy and application access logs are disabled, responses use `no-referrer` and `no-store`, and pages load no external resources. |
| The database is copied | Sensitive fields are encrypted. Passwords use Argon2id, while access tokens and fingerprints are stored as keyed hashes. Keys remain outside the database in `.env`. |
| A browser request changes data without permission | State-changing forms require a session-bound CSRF token. Account-owner and technical-admin sessions are separate. |
| Entered text injects executable browser code | Jinja escapes normal output, notification text is plain text, public Markdown uses a small allowlist, and a restrictive Content Security Policy is sent. |
| Confidential data appears in logs | Logs contain technical events only. Message text, contact details, credentials, cookies, access tokens, and inbound email content must never be logged. |
| The SMTP server is temporarily unavailable | Encrypted temporary payloads are retried a limited number of times with increasing delays and are removed after delivery or expiry. |
| Two schedulers process the same SQLite database | The supported deployment runs exactly one scheduler and uses database constraints for local consistency. |
| A claimed real-world event is false | SilentRelay forwards submitted text but cannot verify whether it is true. Messages do not claim independent verification. |
| The server becomes unavailable | Health checks, persistent storage, and documented backup and restore procedures support recovery. |

## Email delivery reports and the technical mailbox

SMTP acceptance does not prove that an email reached its final mailbox.
SilentRelay can therefore process later delivery-status reports through the
technical sender mailbox.

When this function is enabled:

1. Each outgoing message receives a random correlation code in its SMTP
   envelope sender.
2. Only a keyed hash of that code and minimal local status data are stored.
3. The scheduler opens the explicitly configured inbox through encrypted IMAP.
4. Only standards-compliant delivery-status data matching an outstanding code
   can change a contact or delivery status.
5. Every incoming message is permanently deleted after successful inspection.

The visible sender address does not contain the code. SilentRelay does not
retain inbound message bodies, returned original messages, attachments,
addresses found in reports, or inbound message identifiers.

Incoming email remains untrusted. A malformed report, forged report, ordinary
reply, or spam message is not interpreted merely because it arrived in the
mailbox. A secret correlation code makes blind forgery difficult, but someone
who learns a valid outstanding code could still submit a matching forged
report. The resulting effect is limited to marking a contact method
unconfirmed; the account owner must then verify it again.

The mailbox is deliberately destructive and must be used only by SilentRelay.
The administration area requires acknowledgement of the exact address and
warns that replies, spam, malformed mail, and processed reports are deleted.
The IMAP connection test opens the inbox read-only and does not fetch or delete
messages.

A missing delivery report is not proof of successful delivery. Regular contact
confirmation remains necessary.

## Remaining risks

The following limitations are accepted:

- A compromised host can access runtime secrets and decrypted data.
- Mail servers and recipient systems receive plaintext notification content.
- SMTP delivery cannot be atomically committed together with SQLite. A crash
  in a narrow window can cause a duplicate external email; see ADR 0013.
- Delivery reports can be missing, delayed, non-standard, or deliberately
  forged by someone who knows a current correlation code.
- Physical copies of QR codes remain usable until their access is replaced or
  deactivated.
- Version 1 provides no password or account-owner access recovery.
- Losing the `.env` keys makes encrypted database fields unreadable.

## Rules for contributors

Changes must preserve these boundaries:

- never log or persist more personal data than the feature requires;
- never reveal recipient identities or counts to a trusted person;
- keep reusable access tokens out of the database;
- keep sensitive stored values encrypted;
- keep CSRF protection on every state-changing browser action;
- keep secret-bearing paths out of access logs;
- remove temporary message content according to the lifecycle rules;
- treat all inbound email and operator-entered text as untrusted; and
- document a changed security or privacy decision in a new ADR.

Prefer the smallest design that solves the problem. New external providers,
analytics, recovery mechanisms, or administrative views require a separate
privacy and security review before implementation.
