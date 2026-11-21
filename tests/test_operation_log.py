from mcp_client.client import ToolCall, ToolResult
from services.operation_log import JournalEntry
from services.requests_service import DeniedCall


def test_operation_log_redacts_credentials_without_modifying_actual_event():
    key = "private-key-value"
    original = ToolCall(
        "read_range",
        {"range_ref": "A1:D4", "nested": [{"X-API-KEY": "external-credential"}, key]},
        ToolResult("success", {"data": [[101, key]], "token": "other-token"}),
        0.08,
    )
    rejected = DeniedCall("write_cell", f"Отклонено {key}")

    entry = JournalEntry.start(f"Запрос {key}", (original,), "pending", f"Итог {key}", api_key=key)
    updated = entry.update("error", f"Ошибка {key}", api_key=key, events=(rejected,))

    assert all(secret not in repr(updated) for secret in (key, "external-credential", "other-token"))
    assert updated.request == "Запрос [скрыто]"
    assert updated.outcome == "Ошибка [скрыто]"
    assert updated.events[0].result.data["data"] == [[101, "[скрыто]"]]
    assert updated.events[1] == DeniedCall("write_cell", "Отклонено [скрыто]")
    assert original.result.data["data"] == [[101, key]]
    assert len(entry.events) == 1
