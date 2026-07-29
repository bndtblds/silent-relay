import asyncio
import html
import re
import sys
import time
import types
from enum import Enum

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import main
from app.config import Settings, get_settings
from app.database import engine
from app.entitlements import (
    AccountCreatedContext,
    AllowAllEntitlementProvider,
    EntitlementProviderConfigurationError,
    EntitlementProviderUnavailableError,
    RegistrationContext,
    RegistrationDecision,
    load_entitlement_provider,
    registration_policy,
)
from app.models import Account


@pytest.fixture(autouse=True)
def isolate_rate_limits():
    main._requests.clear()
    yield
    main._requests.clear()


def csrf_from(response_text: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response_text)
    assert match
    return html.unescape(match.group(1))


class PolicyOnlyProvider:
    def __init__(self, decision: RegistrationDecision = RegistrationDecision.allow):
        self.decision = decision

    async def registration_policy(
        self, context: RegistrationContext
    ) -> RegistrationDecision:
        return self.decision


class CountingProvider(PolicyOnlyProvider):
    def __init__(self):
        super().__init__()
        self.policy_calls = 0
        self.contexts: list[RegistrationContext] = []

    async def registration_policy(
        self, context: RegistrationContext
    ) -> RegistrationDecision:
        self.policy_calls += 1
        self.contexts.append(context)
        return await super().registration_policy(context)


class RecordingProvider(PolicyOnlyProvider):
    def __init__(self):
        super().__init__()
        self.created: list[AccountCreatedContext] = []

    async def on_account_created(self, context: AccountCreatedContext) -> None:
        self.created.append(context)


class SlowProvider(PolicyOnlyProvider):
    async def registration_policy(
        self, context: RegistrationContext
    ) -> RegistrationDecision:
        await asyncio.sleep(0.05)
        return RegistrationDecision.allow


class FailingProvider(PolicyOnlyProvider):
    async def registration_policy(
        self, context: RegistrationContext
    ) -> RegistrationDecision:
        raise RuntimeError("provider failed")


class FailingHookProvider(PolicyOnlyProvider):
    async def on_account_created(self, context: AccountCreatedContext) -> None:
        raise RuntimeError("hook failed")


class CancellationResistantProvider(PolicyOnlyProvider):
    def __init__(self):
        super().__init__()
        self.cancellation_received = False

    async def registration_policy(
        self, context: RegistrationContext
    ) -> RegistrationDecision:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancellation_received = True
            await asyncio.sleep(10)
        return RegistrationDecision.allow


def post_registration(client: TestClient, language: str = "de"):
    form = client.get("/account/create")
    return client.post(
        "/account/create",
        data={"csrf": csrf_from(form.text), "language_code": language},
    )


def account_count() -> int:
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(Account)) or 0


def test_default_provider_is_allow_all_and_self_hosted_start_succeeds():
    assert Settings.model_fields["entitlement_provider"].default == "allow_all"
    with TestClient(main.app):
        assert isinstance(
            main.app.state.entitlement_provider, AllowAllEntitlementProvider
        )


def test_allow_all_provider_allows_registration():
    decision = asyncio.run(
        AllowAllEntitlementProvider().registration_policy(
            RegistrationContext()
        )
    )
    assert decision == RegistrationDecision.allow


def test_explicit_module_factory_can_load_policy_only_provider(monkeypatch):
    module = types.ModuleType("test_entitlement_extension")
    module.create_provider = lambda: PolicyOnlyProvider()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    provider = load_entitlement_provider(
        "test_entitlement_extension:create_provider"
    )
    assert isinstance(provider, PolicyOnlyProvider)
    assert not hasattr(provider, "on_account_created")


@pytest.mark.parametrize(
    ("factory_value", "error_class"),
    [
        (None, "TypeError"),
        (lambda: object(), None),
    ],
)
def test_non_callable_factory_or_incompatible_provider_is_rejected(
    monkeypatch, factory_value, error_class
):
    module = types.ModuleType("invalid_entitlement_extension")
    module.create_provider = factory_value  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(EntitlementProviderConfigurationError) as raised:
        load_entitlement_provider(
            "invalid_entitlement_extension:create_provider"
        )
    if error_class:
        assert error_class in str(raised.value)
    else:
        assert "must implement registration_policy" in str(raised.value)


def test_missing_factory_is_rejected(monkeypatch):
    module = types.ModuleType("entitlement_extension_without_factory")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(EntitlementProviderConfigurationError, match="AttributeError"):
        load_entitlement_provider(
            "entitlement_extension_without_factory:create_provider"
        )


def test_factory_exception_is_sanitized(monkeypatch):
    module = types.ModuleType("failing_entitlement_extension")

    def failing_factory():
        raise RuntimeError("secret-provider-value")

    module.create_provider = failing_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(EntitlementProviderConfigurationError) as raised:
        load_entitlement_provider(
            "failing_entitlement_extension:create_provider"
        )
    assert "RuntimeError" in str(raised.value)
    assert "secret-provider-value" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_async_factory_and_synchronous_policy_are_rejected(monkeypatch):
    async_factory_module = types.ModuleType("async_entitlement_extension")

    async def async_factory():
        return PolicyOnlyProvider()

    async_factory_module.create_provider = async_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, async_factory_module.__name__, async_factory_module)
    with pytest.raises(EntitlementProviderConfigurationError, match="TypeError"):
        load_entitlement_provider("async_entitlement_extension:create_provider")

    sync_policy_module = types.ModuleType("sync_policy_extension")

    class SyncProvider:
        def registration_policy(self, context):
            return RegistrationDecision.allow

    sync_policy_module.create_provider = lambda: SyncProvider()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, sync_policy_module.__name__, sync_policy_module)
    with pytest.raises(EntitlementProviderConfigurationError, match="async method"):
        load_entitlement_provider("sync_policy_extension:create_provider")


def test_invalid_policy_or_hook_signature_is_rejected(monkeypatch):
    policy_module = types.ModuleType("invalid_policy_signature_extension")

    class InvalidPolicyProvider:
        async def registration_policy(self):
            return RegistrationDecision.allow

    policy_module.create_provider = lambda: InvalidPolicyProvider()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, policy_module.__name__, policy_module)
    with pytest.raises(EntitlementProviderConfigurationError, match="one registration"):
        load_entitlement_provider(
            "invalid_policy_signature_extension:create_provider"
        )

    hook_module = types.ModuleType("invalid_hook_signature_extension")

    class InvalidHookProvider(PolicyOnlyProvider):
        async def on_account_created(self):
            pass

    hook_module.create_provider = lambda: InvalidHookProvider()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, hook_module.__name__, hook_module)
    with pytest.raises(EntitlementProviderConfigurationError, match="account-created"):
        load_entitlement_provider(
            "invalid_hook_signature_extension:create_provider"
        )


@pytest.mark.parametrize(
    "provider_path",
    [
        "./extension.py:create_provider",
        r"C:\extension.py:create_provider",
        ".relative:create_provider",
        "extension:create-provider",
        "extension:create_provider:extra",
    ],
)
def test_file_relative_and_malformed_provider_paths_are_rejected(provider_path):
    with pytest.raises(
        EntitlementProviderConfigurationError,
        match="explicit 'module:factory' path",
    ):
        load_entitlement_provider(provider_path)


def test_invalid_provider_configuration_is_understandable():
    with pytest.raises(
        EntitlementProviderConfigurationError,
        match="Could not load entitlement provider 'missing_extension:create_provider'",
    ):
        load_entitlement_provider("missing_extension:create_provider")


def test_invalid_provider_aborts_application_start():
    settings = main.settings
    previous = settings.entitlement_provider
    settings.entitlement_provider = "missing_extension:create_provider"
    try:
        with pytest.raises(
            EntitlementProviderConfigurationError,
            match="Could not load entitlement provider",
        ):
            with TestClient(main.app):
                pass
    finally:
        settings.entitlement_provider = previous


def test_factory_runs_once_during_startup(monkeypatch):
    module = types.ModuleType("counted_entitlement_extension")
    calls = 0

    def create_provider():
        nonlocal calls
        calls += 1
        return PolicyOnlyProvider()

    module.create_provider = create_provider  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    previous = main.settings.entitlement_provider
    main.settings.entitlement_provider = (
        "counted_entitlement_extension:create_provider"
    )
    try:
        with TestClient(main.app) as client:
            assert client.get("/health/live").status_code == 200
            assert client.get("/account/create").status_code == 200
    finally:
        main.settings.entitlement_provider = previous
    assert calls == 1


def test_provider_can_allow_registration_and_receive_optional_hook():
    provider = RecordingProvider()
    with TestClient(main.app) as client:
        main.app.state.entitlement_provider = provider
        response = post_registration(client, "en")
    assert response.status_code == 200
    assert account_count() == 1
    assert len(provider.created) == 1
    assert provider.created[0].account_id
    assert vars(provider.created[0]) == {"account_id": provider.created[0].account_id}


def test_policy_only_provider_can_allow_registration():
    provider = CountingProvider()
    with TestClient(main.app) as client:
        main.app.state.entitlement_provider = provider
        response = post_registration(client)
    assert response.status_code == 200
    assert account_count() == 1
    assert len(provider.contexts) == 1
    assert vars(provider.contexts[0]) == {}


def test_provider_can_deny_registration_without_creating_account():
    with TestClient(main.app) as client:
        main.app.state.entitlement_provider = PolicyOnlyProvider(
            RegistrationDecision.deny
        )
        response = post_registration(client)
    assert response.status_code == 403
    assert "Kontoerstellung nicht möglich" in response.text
    assert account_count() == 0


def test_denial_and_unavailable_pages_are_neutral_in_english():
    with TestClient(main.app) as client:
        main.app.state.entitlement_provider = PolicyOnlyProvider(
            RegistrationDecision.deny
        )
        denied = post_registration(client, "en")
        main.app.state.entitlement_provider = FailingProvider()
        unavailable = post_registration(client, "en")
    assert denied.status_code == 403
    assert "Account creation unavailable" in denied.text
    assert unavailable.status_code == 503
    assert "temporarily unavailable" in unavailable.text
    for response in (denied, unavailable):
        assert "provider" not in response.text.casefold()
        assert "payment" not in response.text.casefold()


def test_closed_core_registration_does_not_call_provider():
    settings = get_settings()
    previous = settings.account_creation_enabled
    provider = CountingProvider()
    try:
        with TestClient(main.app) as client:
            form = client.get("/account/create")
            settings.account_creation_enabled = False
            main.app.state.entitlement_provider = provider
            response = client.post(
                "/account/create",
                data={"csrf": csrf_from(form.text), "language_code": "de"},
            )
    finally:
        settings.account_creation_enabled = previous
    assert response.status_code == 404
    assert provider.policy_calls == 0
    assert account_count() == 0


class OtherDecision(str, Enum):
    allow = "allow"


@pytest.mark.parametrize(
    "invalid_decision", [True, None, "allow", OtherDecision.allow]
)
def test_invalid_decision_fails_closed_without_account(invalid_decision):
    provider = PolicyOnlyProvider()
    provider.decision = invalid_decision
    with TestClient(main.app) as client:
        main.app.state.entitlement_provider = provider
        response = post_registration(client)
    assert response.status_code == 503
    assert account_count() == 0


@pytest.mark.parametrize("provider", [SlowProvider(), FailingProvider()])
def test_provider_timeout_or_error_fails_closed(provider):
    settings = get_settings()
    previous = settings.entitlement_provider_timeout_seconds
    settings.entitlement_provider_timeout_seconds = 0.001
    try:
        with TestClient(main.app) as client:
            main.app.state.entitlement_provider = provider
            response = post_registration(client)
    finally:
        settings.entitlement_provider_timeout_seconds = previous
    assert response.status_code == 503
    assert "vorübergehend nicht verfügbar" in response.text
    assert account_count() == 0


def test_timeout_returns_promptly_and_requests_provider_cancellation():
    provider = CancellationResistantProvider()

    async def run():
        started = time.monotonic()
        with pytest.raises(
            EntitlementProviderUnavailableError, match="could not make"
        ):
            await registration_policy(provider, RegistrationContext(), 0.001)
        await asyncio.sleep(0)
        return time.monotonic() - started

    elapsed = asyncio.run(run())
    assert elapsed < 0.1
    assert provider.cancellation_received


def test_parallel_policy_calls_are_independent():
    provider = CountingProvider()

    async def run():
        return await asyncio.gather(
            registration_policy(provider, RegistrationContext(), 1),
            registration_policy(provider, RegistrationContext(), 1),
        )

    decisions = asyncio.run(run())
    assert decisions == [RegistrationDecision.allow, RegistrationDecision.allow]
    assert provider.policy_calls == 2
    assert provider.contexts[0] is not provider.contexts[1]


@pytest.mark.parametrize(
    "invalid_timeout",
    [0, -1, float("nan"), float("inf"), float("-inf")],
)
def test_timeout_must_be_positive_and_finite(invalid_timeout):
    with pytest.raises(ValidationError):
        Settings(entitlement_provider_timeout_seconds=invalid_timeout)


def test_optional_hook_failure_does_not_damage_created_account(caplog):
    with TestClient(main.app) as client:
        main.app.state.entitlement_provider = FailingHookProvider()
        response = post_registration(client)
    assert response.status_code == 200
    assert account_count() == 1
    assert "entitlement_account_created_hook_failed" in caplog.text
