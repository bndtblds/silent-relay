# SilentRelay threat model

SilentRelay protects confidential contact data and notification content against database theft, accidental disclosure through the web interface, logs, browser referrers, and untrusted senders. It does not protect against a fully compromised host that can read process memory and runtime secrets.

## Assets and trust boundaries

- Account-owner and trusted-person URLs are bearer secrets and are stored only as keyed hashes.
- Passwords are Argon2id hashes.
- Names, contact values, temporary messages, and error details are encrypted with Fernet (AES-128-CBC plus HMAC as provided by `cryptography`).
- The reverse proxy, application host, environment or Docker secrets, database volume, and SMTP server are trusted infrastructure.
- A trusted person is not trusted with recipient identity, recipient count, contact data, account identity, or relationship data.

## Main threats and controls

| Threat | Controls |
|---|---|
| Photographed trusted-person QR code | Rotation, revocation, rate limiting, neutral pages, no recipient disclosure |
| Photographed account-owner QR code | Additional password, Argon2id, login lockout, rotation, short server session |
| Browser history/referrer leakage | Token removed after login, `no-referrer`, no external resources, `no-store` |
| Stolen database | Application-level encryption, keyed token hashes and fingerprints, keys stored separately |
| CSRF/session fixation | Session-bound CSRF tokens, new session after login, Strict/HttpOnly/Secure cookies |
| XSS | Jinja auto-escaping, plain text messages, restrictive CSP, no rich text |
| Log disclosure | Access log disabled, JSON technical events only, no request bodies or contact values |
| SMTP outage | Durable encrypted payload, bounded exponential retry, abstract failure status |
| Concurrent scheduler | One SQLite scheduler, unique constraints, short transactions |
| False reports | Secret sender token, rotation and revocation; outgoing mail does not claim truth or verification |
| Server outage | Health checks, persistent volume, documented backup and restore |

## Residual risks

- A compromised host can access runtime keys and decrypted data.
- SMTP and recipient mail systems necessarily receive message plaintext.
- SMTP cannot provide atomic exactly-once delivery with the local database; see ADR 0013.
- There is deliberately no credential recovery in version 1.
