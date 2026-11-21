"""Сквозная приёмка с настроенной моделью и временной книгой."""

import asyncio
import os
from dataclasses import replace

import pytest
from streamlit.testing.v1 import AppTest

from config.settings import PROJECT_ROOT, load_settings
from mcp_client.client import ToolCall
from scripts.init_excel import initialize_workbook
from services.requests_service import RequestsController


@pytest.mark.skipif(os.getenv("EXCEL_AGENT_LIVE_TEST") != "1", reason="Запуск с реальной моделью включается отдельно.")
def test_live_streamlit_workflow(tmp_path, monkeypatch):
    path = tmp_path / "requests.xlsx"
    settings = replace(load_settings(), excel_file_path=path)
    monkeypatch.setenv("EXCEL_FILE_PATH", str(path))
    assert asyncio.run(initialize_workbook(settings))

    async def records():
        async with RequestsController(settings).operation() as operation:
            return await operation.read_table()

    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=150).run()
    assert not app.exception and not app.error

    def submit(prompt):
        app.chat_input[0].set_value(prompt).run()
        assert not app.exception
        last = app.chat_message[-1]
        assert not last.error, last.error[0].value if last.error else ""
        return last.text[0].value

    def visible_ids():
        return app.dataframe[0].value["ID"].tolist()

    def calls(entry):
        return [event.name for event in entry.events if isinstance(event, ToolCall)]

    def check_limits(entry):
        assert len(calls(entry)) <= settings.mcp_max_tool_calls
        for event in entry.events:
            if isinstance(event, ToolCall):
                assert event.arguments["workbook_path"] == str(path)
                assert event.arguments["sheet_name"] == settings.excel_sheet_name

    assert visible_ids() == [101, 102, 103]
    answer = submit("Добавь заявку от Анны Смирновой: ноутбук сильно тормозит.")
    assert "№104" in answer, answer
    initial = (104, "Анна Смирнова", "ноутбук сильно тормозит", "Новый")
    assert asyncio.run(records())[5] == initial
    assert visible_ids() == [101, 102, 103, 104]
    entry = app.session_state.journal[-1]
    assert entry.status == "completed" and calls(entry)[-2:] == ["write_range", "read_range"]
    check_limits(entry)
    print("Live scenario 1 passed: add and verify.")

    answer = submit("Измени её статус на «В работе».")
    assert "№104" in answer and "Новый" in answer and "В работе" in answer, answer
    pending = app.session_state.pending_confirmation
    assert pending is not None and pending.change.request_id == 104
    assert asyncio.run(records())[5] == initial
    entry = app.session_state.journal[pending.journal_index]
    assert entry.status == "pending" and "write_cell" not in calls(entry)
    check_limits(entry)
    before_confirmation = len(entry.events)
    app.button(key=f"confirm_{pending.operation_id}").click().run()
    app.run()
    assert not app.exception and app.session_state.pending_confirmation is None
    assert "изменён" in app.chat_message[-1].text[0].value
    assert asyncio.run(records())[5] == (*initial[:3], "В работе")
    assert app.dataframe[0].value["Статус"].tolist()[-1] == "В работе"
    entry = app.session_state.journal[pending.journal_index]
    assert entry.status == "completed" and calls(entry)[-3:] == ["read_range", "write_cell", "read_range"]
    assert calls(entry).count("write_cell") == 1
    assert len(entry.events) > before_confirmation
    print("Live scenario 2 passed: contextual status change, confirmation and replay protection.")

    answer = submit("Покажи все заявки со статусом «В работе».")
    assert "№103" in answer and "№104" in answer and "— 2." in answer, answer
    assert visible_ids() == [103, 104]
    entry = app.session_state.journal[-1]
    assert entry.status == "completed" and "read_range" in calls(entry)
    assert all(not name.startswith("write_") for name in calls(entry))
    check_limits(entry)
    app.button(key="show_all").click().run()
    assert visible_ids() == [101, 102, 103, 104]
    print("Live scenario 3 passed: search, table filter and reset filter.")

    submit("Измени статус заявки №104 на «Закрыт».")
    pending = app.session_state.pending_confirmation
    assert pending is not None
    app.button(key=f"cancel_{pending.operation_id}").click().run()
    assert not app.exception and app.session_state.pending_confirmation is None
    assert asyncio.run(records())[5][-1] == "В работе"
    entry = app.session_state.journal[pending.journal_index]
    assert entry.status == "cancelled" and "write_cell" not in calls(entry)
    print("Live cancellation passed.")

    async def add_duplicate_name():
        async with RequestsController(settings).operation(allow_add=True) as operation:
            result = await operation.call_tool("write_range", {
                "start_cell": "A6", "data": [[105, "Анна Смирнова", "Принтер", "Новый"]],
            })
            assert result.ok

    asyncio.run(add_duplicate_name())
    answer = submit("Измени статус заявки Анны Смирновой на «Закрыт».")
    assert app.session_state.pending_confirmation is None, answer
    assert "ID" in answer or ("104" in answer and "105" in answer), answer
    rows = asyncio.run(records())
    assert rows[5][-1] == "В работе" and rows[6][-1] == "Новый"
    assert all(not name.startswith("write_") for name in calls(app.session_state.journal[-1]))
    print("Live ambiguity check passed.")
