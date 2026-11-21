"""Состояние подтверждения и выполнение смены статуса."""

from dataclasses import dataclass
from uuid import uuid4

from config.settings import ExcelSettings
from mcp_client.client import McpConnectionError, ToolCall
from services.requests_service import (
    DeniedCall,
    RequestsController,
    RequestsError,
    StaleConfirmation,
    StatusChange,
)
from services.validation import RequestRows, TableError


@dataclass(frozen=True)
class PendingConfirmation:
    operation_id: str
    change: StatusChange
    journal_index: int

    @classmethod
    def create(cls, change: StatusChange, journal_index: int) -> "PendingConfirmation":
        return cls(uuid4().hex, change, journal_index)


@dataclass(frozen=True)
class ConfirmationOutcome:
    text: str
    error: bool
    events: tuple[ToolCall | DeniedCall, ...]
    records: RequestRows | None = None
    replacement: StatusChange | None = None


def confirmation_text(change: StatusChange) -> str:
    request_id, _, description, current_status = change.original
    return (
        f"Подтвердите смену статуса заявки №{request_id} ({description}) "
        f"с «{current_status}» на «{change.target_status}»."
    )


async def confirm_status(settings: ExcelSettings, change: StatusChange) -> ConfirmationOutcome:
    operation = None
    try:
        async with RequestsController(settings).operation() as operation:
            result = await operation.apply_confirmation(change, confirmed=True)
    except StaleConfirmation as error:
        return ConfirmationOutcome(str(error), True, operation.events, operation.records, error.replacement)
    except (McpConnectionError, RequestsError, TableError) as error:
        events = operation.events if operation is not None else ()
        records = operation.records if operation is not None else None
        return ConfirmationOutcome(str(error), True, events, records)

    status = result.data["data"][3]
    if result.data["changed"]:
        text = f"Статус заявки №{change.request_id} изменён на «{status}»."
    else:
        text = f"У заявки №{change.request_id} уже установлен статус «{status}»."
    return ConfirmationOutcome(text, False, operation.events, operation.records)
