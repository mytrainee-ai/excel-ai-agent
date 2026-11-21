"""Записи журнала, безопасные для хранения в сессии Streamlit."""

from dataclasses import dataclass, replace
from typing import Any, Literal

from mcp_client.client import ToolCall
from services.requests_service import DeniedCall


OperationStatus = Literal["completed", "error", "pending", "cancelled", "recorded"]
_REDACTED = "[скрыто]"
_SECRET_FIELDS = {
    "api_key", "apikey", "openai_api_key", "x_api_key", "authorization",
    "password", "secret", "token", "access_token", "refresh_token", "client_secret", "private_key",
}


def _redact(value: Any, api_key: str) -> Any:
    if isinstance(value, str):
        return value.replace(api_key, _REDACTED) if api_key else value
    if isinstance(value, dict):
        return {
            _redact(key, api_key): (
                _REDACTED
                if isinstance(key, str) and key.casefold().replace("-", "_") in _SECRET_FIELDS
                else _redact(item, api_key)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, api_key) for item in value]
    return value


def _safe_event(event: ToolCall | DeniedCall, api_key: str) -> ToolCall | DeniedCall:
    if isinstance(event, DeniedCall):
        return DeniedCall(_redact(event.name, api_key), _redact(event.reason, api_key))
    return replace(
        event,
        name=_redact(event.name, api_key),
        arguments=_redact(event.arguments, api_key),
        result=replace(event.result, data=_redact(event.result.data, api_key)),
    )


@dataclass(frozen=True)
class JournalEntry:
    request: str
    events: tuple[ToolCall | DeniedCall, ...]
    status: OperationStatus
    outcome: str

    @classmethod
    def start(
        cls,
        request: str,
        events: tuple[ToolCall | DeniedCall, ...],
        status: OperationStatus,
        outcome: str,
        *,
        api_key: str,
    ) -> "JournalEntry":
        return cls(
            _redact(request, api_key),
            tuple(_safe_event(event, api_key) for event in events),
            status,
            _redact(outcome, api_key),
        )

    def update(
        self,
        status: OperationStatus,
        outcome: str,
        *,
        api_key: str,
        events: tuple[ToolCall | DeniedCall, ...] = (),
    ) -> "JournalEntry":
        return replace(
            self,
            events=self.events + tuple(_safe_event(event, api_key) for event in events),
            status=status,
            outcome=_redact(outcome, api_key),
        )
