from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Protocol, TypeVar, cast


logger = logging.getLogger("silent_relay")


ENTITLEMENT_PROVIDER_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class RegistrationContext:
    pass


class RegistrationDecision(str, Enum):
    allow = "allow"
    deny = "deny"


@dataclass(frozen=True)
class AccountCreatedContext:
    account_id: str


class EntitlementProvider(Protocol):
    async def registration_policy(
        self, context: RegistrationContext
    ) -> RegistrationDecision: ...


class AccountCreatedHook(Protocol):
    async def on_account_created(self, context: AccountCreatedContext) -> None: ...


class AllowAllEntitlementProvider:
    contract_version = ENTITLEMENT_PROVIDER_CONTRACT_VERSION

    async def registration_policy(
        self, context: RegistrationContext
    ) -> RegistrationDecision:
        return RegistrationDecision.allow


class EntitlementProviderConfigurationError(RuntimeError):
    pass


class EntitlementProviderUnavailableError(RuntimeError):
    pass


_Result = TypeVar("_Result")


def _is_async_callable(value: object) -> bool:
    return inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(
        getattr(value, "__call__", None)
    )


def _accepts_one_context(value: object, context: object) -> bool:
    try:
        inspect.signature(value).bind(context)
    except (TypeError, ValueError):
        return False
    return True


def load_entitlement_provider(configured_provider: str) -> EntitlementProvider:
    provider_path = configured_provider.strip()
    if provider_path == "allow_all":
        return AllowAllEntitlementProvider()

    module_name, separator, factory_name = provider_path.partition(":")
    valid_module = bool(module_name) and all(
        part.isidentifier() for part in module_name.split(".")
    )
    if (
        not separator
        or not valid_module
        or not factory_name.isidentifier()
        or ":" in factory_name
    ):
        raise EntitlementProviderConfigurationError(
            "ENTITLEMENT_PROVIDER must be 'allow_all' or an explicit "
            "'module:factory' path"
        )

    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, factory_name)
        if not callable(factory):
            raise TypeError("configured factory is not callable")
        if _is_async_callable(factory):
            raise TypeError("configured factory must be synchronous")
        provider = factory()
    except Exception as exc:
        raise EntitlementProviderConfigurationError(
            f"Could not load entitlement provider '{provider_path}': "
            f"{type(exc).__name__}"
        ) from None

    try:
        policy = getattr(provider, "registration_policy")
    except Exception as exc:
        raise EntitlementProviderConfigurationError(
            f"Entitlement provider '{provider_path}' must implement "
            f"registration_policy(context): {type(exc).__name__}"
        ) from None
    if not callable(policy):
        raise EntitlementProviderConfigurationError(
            f"Entitlement provider '{provider_path}' must implement "
            "registration_policy(context)"
        )
    if not _is_async_callable(policy):
        raise EntitlementProviderConfigurationError(
            f"Entitlement provider '{provider_path}' must implement "
            "registration_policy(context) as an async method"
        )
    if not _accepts_one_context(policy, RegistrationContext()):
        raise EntitlementProviderConfigurationError(
            f"Entitlement provider '{provider_path}' must accept one "
            "registration context"
        )
    try:
        hook = getattr(provider, "on_account_created", None)
    except Exception as exc:
        raise EntitlementProviderConfigurationError(
            f"Entitlement provider '{provider_path}' has an invalid "
            f"on_account_created capability: {type(exc).__name__}"
        ) from None
    if hook is not None and (not callable(hook) or not _is_async_callable(hook)):
        raise EntitlementProviderConfigurationError(
            f"Entitlement provider '{provider_path}' must implement "
            "on_account_created(context) as an async method when present"
        )
    if hook is not None and not _accepts_one_context(
        hook, AccountCreatedContext(account_id="validation")
    ):
        raise EntitlementProviderConfigurationError(
            f"Entitlement provider '{provider_path}' must accept one "
            "account-created context when the hook is present"
        )
    return cast(EntitlementProvider, provider)


def _consume_task_result(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        pass


async def _call_with_timeout(
    operation: Awaitable[_Result], timeout_seconds: float
) -> _Result:
    task = asyncio.create_task(operation)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise
    if not done:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise TimeoutError
    if task.cancelled():
        raise RuntimeError("provider operation was cancelled")
    return task.result()


async def registration_policy(
    provider: EntitlementProvider,
    context: RegistrationContext,
    timeout_seconds: float,
) -> RegistrationDecision:
    try:
        decision = await _call_with_timeout(
            provider.registration_policy(context), timeout_seconds
        )
    except Exception as exc:
        logger.warning(
            "entitlement_registration_policy_failed: %s", type(exc).__name__
        )
        raise EntitlementProviderUnavailableError(
            "The entitlement provider could not make a registration decision"
        ) from exc
    if not isinstance(decision, RegistrationDecision):
        logger.warning(
            "entitlement_registration_policy_invalid_decision: %s",
            type(decision).__name__,
        )
        raise EntitlementProviderUnavailableError(
            "The entitlement provider returned an invalid registration decision"
        )
    return decision


async def notify_account_created(
    provider: EntitlementProvider,
    context: AccountCreatedContext,
    timeout_seconds: float,
) -> None:
    try:
        hook = getattr(provider, "on_account_created", None)
        if hook is None:
            return
        await _call_with_timeout(
            hook(context), timeout_seconds
        )
    except Exception as exc:
        logger.warning(
            "entitlement_account_created_hook_failed: %s", type(exc).__name__
        )
