# Entitlement provider

SilentRelay has a deliberately small extension point for deciding whether a
public account registration may create an account. It lets an operator apply
deployment-specific registration rules while the open-source core retains
ownership of account and security data.

This is not a general plugin system. Its scope is limited to the contracts
documented below. The default installation uses the built-in allow-all
provider.

## Public interface

Providers implement the asynchronous `EntitlementProvider` protocol:

```python
class EntitlementProvider(Protocol):
    async def registration_policy(
        self,
        context: RegistrationContext,
    ) -> RegistrationDecision:
        ...
```

`RegistrationDecision` has exactly two values: `allow` and `deny`.
`RegistrationContext` is currently empty because the decision needs no
registration data. SilentRelay does not pass the selected language, an email
address, IP address, browser metadata, contacts, partners, trusted persons,
recipients, notification data, or access tokens.

A provider may additionally implement the separate `AccountCreatedHook`:

```python
class AccountCreatedHook(Protocol):
    async def on_account_created(
        self,
        context: AccountCreatedContext,
    ) -> None:
        ...
```

The hook is optional. Its context contains only the account's internal UUIDv7
identifier as the same `str` stored in `Account.id`. The identifier remains
unchanged for the lifetime of that account. It runs after SilentRelay commits
the account. A hook failure is logged and does not roll back or damage the
account.

## Configuration and loading

The default configuration is:

```env
ENTITLEMENT_PROVIDER=allow_all
ENTITLEMENT_PROVIDER_TIMEOUT_SECONDS=2
```

`allow_all` makes no external checks. Whether account creation is available is
controlled separately under **System settings** in the administration area.
The timeout must be greater than zero and no greater than 30 seconds.

An alternative provider must already be installed in the application
environment and is selected with an explicit module and factory:

```env
ENTITLEMENT_PROVIDER=example_extension.entitlements:create_provider
```

The factory must be synchronous, takes no arguments, and returns an
`EntitlementProvider`. Asynchronous factories are rejected during startup.
The provider methods themselves must be asynchronous. The factory loads its
own deployment-specific configuration. SilentRelay does not scan writable
directories, accept file paths, download code, or discover providers
automatically.

SilentRelay loads the provider and factory during application startup. A
missing module, missing or non-callable factory, factory exception, or
incompatible provider produces a clear startup error and prevents the web
application from starting. The synchronous factory runs exactly once during
each application startup. The returned provider instance is shared by
concurrent requests and must therefore be safe for concurrent asynchronous
calls.

## Runtime failure behavior

SilentRelay consults the provider after validating the public form and before
calling `AccountService.create()`. Disabled account creation in **System
settings** is checked first and prevents the provider from being called.

- `allow`: the existing core service creates the account.
- `deny`: SilentRelay returns a neutral HTTP 403 response and creates no
  account.
- Provider exception, invalid decision, or timeout: SilentRelay fails closed,
  returns a neutral HTTP 503 response, and creates no account.

HTTP 403 follows SilentRelay's existing convention for a request that is
understood but not permitted. HTTP 503 distinguishes a temporary inability to
make the required decision. Both responses use neutral core-owned text and
never expose the provider or its reason.

The timeout covers only each provider method call; it does not cover the core
account commit. The same timeout bounds the optional post-creation hook. Hook
failures are best-effort failures because the core account is already
committed. Runtime logs contain a technical event and exception class, not
registration or account data.

This boundary cannot make a remote decision and a local database commit
atomic. Providers should treat checks as short-lived decisions and make
post-creation processing idempotent by account ID. On timeout SilentRelay
requests cancellation and returns without creating an account. A provider that
suppresses cancellation, or an external operation already accepted by another
system, can still produce later side effects that SilentRelay cannot undo.
Providers must handle cancellation, bound their own I/O, and make their own
side effects idempotent.

## Test-only example

```python
from app.entitlements import RegistrationDecision


class TestProvider:
    async def registration_policy(self, context):
        return RegistrationDecision.allow


def create_provider():
    return TestProvider()
```

This provider intentionally implements no account-created hook and is valid.

## Provider-owned state and contract limits

Extensions should keep their own data and migrations outside the SilentRelay
schema. Internal account IDs are the only supported shared identifier. Email
addresses must not be durable cross-system keys.

Only capabilities explicitly defined by the public entitlement contracts are
supported. Extensions must not depend on undocumented routes, models, database
tables, implementation details, or data that SilentRelay does not deliberately
include in a provider context.
