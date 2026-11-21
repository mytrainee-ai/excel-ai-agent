import asyncio
import os

import pytest
from dotenv import dotenv_values

from config.settings import PROJECT_ROOT, ExcelSettings
from scripts.init_excel import initialize_workbook
from services.confirmations import confirm_status
from services.requests_service import DeniedCall, RequestsController, StatusChange


def test_confirmed_status_rechecks_sheet_and_replaces_stale_request(tmp_path):
    interpreter = os.environ.get("MCP_PYTHON_PATH")
    if interpreter is None:
        interpreter = dotenv_values(PROJECT_ROOT / ".env.example")["MCP_PYTHON_PATH"]
    if not interpreter or not (PROJECT_ROOT / interpreter).is_file():
        pytest.skip("Для интеграционного теста нужен Excel MCP Server.")
    settings = ExcelSettings(
        tmp_path / "requests.xlsx", "Заявки", PROJECT_ROOT / interpreter, 10, 15, 30
    )
    controller = RequestsController(settings)

    async def prepare(status):
        async with controller.operation() as operation:
            change = await operation.call_tool("write_cell", {"cell": "D4", "value": status})
            assert isinstance(change, StatusChange)
            return change

    async def scenario():
        assert await initialize_workbook(settings)
        first = await prepare("Закрыт")

        second = await prepare("Новый")
        intervening = await confirm_status(settings, second)
        assert not intervening.error

        stale = await confirm_status(settings, first)
        assert stale.error and stale.replacement is not None
        assert stale.records[4] == (*first.original[:3], "Новый")
        assert stale.replacement.original == stale.records[4]
        assert [event.name for event in stale.events if not isinstance(event, DeniedCall)] == ["read_range"]

        resolved = await confirm_status(settings, stale.replacement)
        assert not resolved.error
        assert resolved.records[4] == (*first.original[:3], "Закрыт")
        assert [event.name for event in resolved.events] == ["read_range", "write_cell", "read_range"]

        repeated = await confirm_status(settings, stale.replacement)
        assert repeated.error and "уже использовано" in repeated.text
        assert all(isinstance(event, DeniedCall) for event in repeated.events)

    asyncio.run(scenario())
