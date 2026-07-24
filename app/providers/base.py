from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DeliveryResult:
    successful: bool
    permanent_failure: bool = False
    message_id: str | None = None
    error_class: str | None = None


class NotificationProvider(Protocol):
    channel: str

    def send(self, recipient: str, subject: str, body: str) -> DeliveryResult: ...
