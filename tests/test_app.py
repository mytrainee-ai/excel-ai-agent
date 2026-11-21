import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from streamlit.testing.v1 import AppTest

from agent import agent
from agent.agent import AgentReply
from config.settings import PROJECT_ROOT
from mcp_client.client import ToolCall, ToolResult
from services import confirmations, requests_service
from services.confirmations import ConfirmationOutcome
from services.requests_service import DeniedCall, RequestsError, StatusChange, _SessionScope


_SEED = {
    2: (101, "Иван Петров", "Принтер", "Новый"),
    3: (102, "Елена Кузнецова", "Wi-Fi", "Закрыт"),
    4: (103, "Мария Соколова", "Почта", "В работе"),
}


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    path = tmp_path / "requests.xlsx"
    for key, value in {
        "OPENAI_API_KEY": "test-key",
        "OPENAI_MODEL": "test-model",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "MCP_PYTHON_PATH": sys.executable,
        "MCP_MAX_TOOL_CALLS": "10",
        "MCP_TOOL_TIMEOUT_SECONDS": "15",
        "MCP_CONNECT_TIMEOUT_SECONDS": "30",
        "EXCEL_FILE_PATH": str(path),
        "EXCEL_SHEET_NAME": "Заявки",
    }.items():
        monkeypatch.setenv(key, value)

    read = AsyncMock(return_value=_SEED)
    run_turn = AsyncMock()
    event = ToolCall("read_range", {"range_ref": "A1:D1002"}, ToolResult("success"), 0.01)

    @asynccontextmanager
    async def operation():
        yield SimpleNamespace(read_table=read, events=(event,))

    monkeypatch.setattr(
        requests_service, "RequestsController", lambda settings: SimpleNamespace(operation=operation)
    )
    monkeypatch.setattr(agent, "run_turn", run_turn)
    return AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=15), read, run_turn


def _ids(app):
    assert not app.exception
    return app.dataframe[0].value["ID"].tolist()


def test_chat_filter_refresh_and_reset(dashboard):
    app, read, run_turn = dashboard
    app.run()
    assert _ids(app) == [101, 102, 103]
    assert read.await_count == 1

    run_turn.return_value = AgentReply(
        "Найдено заявок со статусом «В работе» — 1.\n№103",
        records=_SEED,
        filter_status="В работе",
    )
    app.chat_input[0].set_value("Покажи заявки в работе.").run()
    assert _ids(app) == [103]
    assert [message.name for message in app.chat_message] == ["user", "assistant"]
    assert "№103" in app.chat_message[1].text[0].value
    assert read.await_count == 1

    read.return_value = _SEED | {5: (104, "Анна Смирнова", "Ноутбук", "В работе")}
    app.button(key="refresh_table").click().run()
    assert _ids(app) == [103, 104]
    assert read.await_count == 2

    app.button(key="show_all").click().run()
    assert _ids(app) == [101, 102, 103, 104]
    assert run_turn.await_count == 1
    assert read.await_count == 2
    assert len(app.session_state.journal) == 3


def test_verified_addition_updates_table_and_rerun_does_not_repeat_request(dashboard):
    app, read, run_turn = dashboard
    app.run()
    run_turn.return_value = AgentReply(
        "Заявка №104 добавлена.",
        records=_SEED | {5: (104, "Анна Смирнова", "Ноутбук", "Новый")},
    )

    app.chat_input[0].set_value("Добавь заявку Анны. Ноутбук тормозит.").run()
    assert _ids(app) == [101, 102, 103, 104]
    assert run_turn.await_args.kwargs == {"allow_add": True, "history": [], "last_request_id": None}
    app.run()

    assert read.await_count == 1
    assert run_turn.await_count == 1
    assert len(app.chat_message) == 2


def test_errors_preserve_last_table_history_and_journal(dashboard):
    app, read, run_turn = dashboard
    app.run()
    read.side_effect = RequestsError("Не удалось прочитать книгу.")
    app.button(key="refresh_table").click().run()
    assert _ids(app) == [101, 102, 103]
    assert "последнего успешного чтения" in app.caption[0].value

    run_turn.return_value = AgentReply("OpenAI недоступен.", error=True)
    app.chat_input[0].set_value("Есть новые заявки?").run()
    assert _ids(app) == [101, 102, 103]
    assert "OpenAI недоступен." in app.chat_message[1].error[0].value
    assert len(app.session_state.journal) == 3
    assert app.session_state.journal[0].events[0].name == "read_range"
    assert app.session_state.journal[1].status == app.session_state.journal[2].status == "error"


def _change(record=_SEED[4]):
    return StatusChange(record, "Закрыт", PROJECT_ROOT / "data/requests.xlsx", "Заявки", _SessionScope(True))


def _open_confirmation(app, run_turn, change):
    event = ToolCall("read_range", {}, ToolResult("success"), 0.01)
    run_turn.return_value = AgentReply(
        confirmations.confirmation_text(change), confirmation=change, events=(event,)
    )
    app.chat_input[0].set_value("Закрой заявку.").run()
    assert not app.exception
    return app.session_state.pending_confirmation


def test_confirm_consumes_operation_and_updates_same_journal(dashboard, monkeypatch):
    app, read, run_turn = dashboard
    change = _change()
    records = _SEED | {4: (*change.original[:3], change.target_status)}
    write = ToolCall("write_cell", {}, ToolResult("success"), 0.02)
    execute = AsyncMock(return_value=ConfirmationOutcome("Статус заявки №103 изменён.", False, (write,), records))
    monkeypatch.setattr(confirmations, "confirm_status", execute)
    app.run()

    pending = _open_confirmation(app, run_turn, change)
    assert "№103" in app.chat_message[1].text[0].value
    assert app.session_state.journal[pending.journal_index].status == "pending"
    execute.assert_not_awaited()
    app.button(key=f"confirm_{pending.operation_id}").click().run()
    app.run()

    assert not app.exception and app.session_state.pending_confirmation is None
    assert pending.operation_id in app.session_state.completed_operation_ids
    assert app.dataframe[0].value["Статус"].tolist()[-1] == "Закрыт"
    entry = app.session_state.journal[pending.journal_index]
    assert entry.status == "completed"
    assert [event.name for event in entry.events] == ["read_range", "write_cell"]
    assert "Статус заявки №103 изменён." in entry.outcome
    execute.assert_awaited_once()
    assert run_turn.await_count == read.await_count == 1


def test_cancel_does_not_run_confirmation(dashboard, monkeypatch):
    app, _, run_turn = dashboard
    execute = AsyncMock()
    monkeypatch.setattr(confirmations, "confirm_status", execute)
    app.run()
    pending = _open_confirmation(app, run_turn, _change())

    app.button(key=f"cancel_{pending.operation_id}").click().run()

    assert not app.exception and app.session_state.pending_confirmation is None
    assert app.dataframe[0].value["Статус"].tolist()[-1] == "В работе"
    assert "отменена" in app.chat_message[-1].text[0].value
    assert app.session_state.journal[pending.journal_index].status == "cancelled"
    assert len(app.session_state.journal[pending.journal_index].events) == 1
    execute.assert_not_awaited()


def test_stale_confirmation_requires_new_click(dashboard, monkeypatch):
    app, _, run_turn = dashboard
    replacement = _change((103, "Мария Соколова", "Новое описание", "В работе"))
    execute = AsyncMock(side_effect=[
        ConfirmationOutcome("Данные заявки изменились.", True, (), replacement=replacement),
        ConfirmationOutcome("Статус заявки №103 изменён.", False, ()),
    ])
    monkeypatch.setattr(confirmations, "confirm_status", execute)
    app.run()
    first = _open_confirmation(app, run_turn, _change())
    app.button(key=f"confirm_{first.operation_id}").click().run()

    second = app.session_state.pending_confirmation
    assert second.operation_id != first.operation_id
    assert second.journal_index == first.journal_index
    assert app.session_state.journal[second.journal_index].status == "pending"
    assert "Новое описание" in app.chat_message[-1].text[0].value
    execute.assert_awaited_once()
    app.button(key=f"confirm_{second.operation_id}").click().run()
    assert execute.await_count == 2


def test_followup_passes_verified_id_and_visible_messages(dashboard):
    app, _, run_turn = dashboard
    app.run()
    run_turn.return_value = AgentReply("Заявка №104 добавлена.", request_id=104)
    app.chat_input[0].set_value("Добавь заявку Анны. Ноутбук тормозит.").run()
    run_turn.return_value = AgentReply("Укажите новый статус.")

    app.chat_input[0].set_value("Измени её статус.").run()

    assert run_turn.await_args.kwargs["last_request_id"] == 104
    assert run_turn.await_args.kwargs["history"] == [
        ("user", "Добавь заявку Анны. Ноутбук тормозит."),
        ("assistant", "Заявка №104 добавлена."),
    ]


def test_sidebar_displays_actual_calls_and_distinguishes_denials(dashboard):
    app, _, run_turn = dashboard
    app.run()
    events = (
        DeniedCall("delete_sheet", "Инструмент запрещён."),
        ToolCall("read_cell", {"cell": "D2"}, ToolResult("success", {"value": "Новый"}), 0.012),
    )
    run_turn.return_value = AgentReply("Новый", events=events)
    app.chat_input[0].set_value("Какой статус у №101?").run()

    assert not app.exception
    assert app.sidebar.subheader[0].value == "Журнал MCP"
    entry = app.sidebar.expander[0]
    texts = [element.value for element in entry.text]
    assert texts[0] == "Какой статус у №101?"
    assert "delete_sheet · отклонён контроллером" in texts
    assert "read_cell · успех · 12 мс" in texts
    payload, = entry.get("json")
    assert json.loads(payload.proto.body) == {
        "Аргументы": {"cell": "D2"},
        "Ответ MCP": {"value": "Новый"},
    }


def test_sidebar_conceals_credentials_and_ignores_fabricated_mcp_text(dashboard):
    app, _, run_turn = dashboard
    app.run()
    events = (
        ToolCall(
            "read_cell", {"cell": "A1"},
            ToolResult("tool_error", {"message": "test-key", "Authorization": "another-secret"}), 0.012,
        ),
    )
    run_turn.return_value = AgentReply("Дополнительно вызван delete_sheet.", events=events)
    app.chat_input[0].set_value("Покажи ячейку.").run()

    assert not app.exception
    entry = app.sidebar.expander[0]
    assert all("delete_sheet" not in element.value for element in entry.text)
    assert "read_cell · ошибка инструмента · 12 мс" in [element.value for element in entry.text]
    payload, = entry.get("json")
    assert json.loads(payload.proto.body)["Ответ MCP"] == {
        "message": "[скрыто]",
        "Authorization": "[скрыто]",
    }
    assert "test-key" not in repr(app.session_state.journal[-1])


def test_sidebar_preserves_earlier_journal_entries_on_rerun(dashboard):
    app, read, _ = dashboard
    app.run()
    events = app.session_state.journal[0].events
    app.session_state.journal = [("Запрос из открытой сессии", events)]

    app.run()

    assert not app.exception and read.await_count == 1
    assert app.session_state.journal[0].status == "recorded"
    assert app.session_state.journal[0].events == events
    assert "Запрос из открытой сессии" in app.sidebar.expander[0].text[0].value
