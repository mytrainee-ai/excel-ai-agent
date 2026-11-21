import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values
from mcp.types import CallToolResult, Implementation

from config.settings import PROJECT_ROOT, ExcelSettings
from mcp_client.client import McpClient, McpSession
from scripts import reset_excel
from scripts.init_excel import INITIAL_REQUESTS, initialize_workbook
from services.requests_service import RequestsController
from services.validation import HEADERS, LAST_ROW


def test_reset_command_cancellation_does_not_connect(tmp_path, monkeypatch, capsys):
    settings = ExcelSettings(tmp_path / "requests.xlsx", "Заявки", tmp_path / "missing.exe", 10, 15, 30)
    monkeypatch.setattr(reset_excel, "load_excel_settings", lambda: settings)
    monkeypatch.setattr("builtins.input", lambda prompt: "нет")
    reset = AsyncMock()
    monkeypatch.setattr(reset_excel, "reset_workbook", reset)

    assert reset_excel.main() == 0
    assert "Сброс отменён" in capsys.readouterr().out
    reset.assert_not_awaited()


def test_reset_service_requires_explicit_confirmation(tmp_path):
    settings = ExcelSettings(tmp_path / "requests.xlsx", "Заявки", tmp_path / "missing.exe", 10, 15, 30)
    with pytest.raises(reset_excel.ResetError, match="явное подтверждение"):
        asyncio.run(reset_excel.reset_workbook(settings))


def test_reset_keeps_backup_when_final_verification_fails(tmp_path):
    settings = ExcelSettings(tmp_path / "requests.xlsx", "Заявки", tmp_path / "missing.exe", 10, 15, 30)
    rows = [[None] * 4 for _ in range(LAST_ROW + 1)]
    rows[:4] = [list(row) for row in (HEADERS, *INITIAL_REQUESTS)]

    def reply(**data):
        return CallToolResult(content=[], structured_content=data)

    sdk = SimpleNamespace(call_tool=AsyncMock(side_effect=[
        reply(sheets=["Sheet", "Заявки"]),
        reply(success=True),
        ConnectionError("write response lost"),
        reply(data=rows),
        reply(success=True),
        reply(success=True),
        reply(success=False),
    ]))
    session = McpSession(sdk, Implementation(name="fake", version="0"), (), 1)

    with pytest.raises(reset_excel.ResetError, match=r"Не удалось перечитать лист.+__reset_old_"):
        asyncio.run(reset_excel._SheetReset(session, settings).run("Заявки"))

    names = [event.name for event in session.calls]
    assert names[-1] == "read_range"
    assert names.count("write_range") == 1
    assert "delete_sheet" not in names


def test_confirmed_reset_restores_target_and_preserves_other_sheet(tmp_path, monkeypatch, capsys):
    interpreter = os.environ.get("MCP_PYTHON_PATH")
    if interpreter is None:
        interpreter = dotenv_values(PROJECT_ROOT / ".env.example")["MCP_PYTHON_PATH"]
    if not interpreter or not (PROJECT_ROOT / interpreter).is_file():
        pytest.skip("Для интеграционного теста нужен Excel MCP Server.")
    settings = ExcelSettings(
        tmp_path / "requests.xlsx", "Заявки", PROJECT_ROOT / interpreter, 10, 15, 30
    )
    client = McpClient(settings.mcp_python_path)
    workbook = str(settings.excel_file_path)

    async def prepare():
        assert await initialize_workbook(settings)
        async with client.operation() as operation:
            assert (await operation.call_tool("write_cell", {
                "workbook_path": workbook, "sheet_name": "Sheet", "cell": "A1", "value": "Сохранить",
            })).ok
            assert (await operation.call_tool("write_range", {
                "workbook_path": workbook, "sheet_name": "Заявки", "start_cell": "A5",
                "data": [[104, "Анна Смирнова", "Ноутбук", "Новый"]],
            })).ok

    async def verify():
        async with RequestsController(settings).operation() as operation:
            assert await operation.read_table() == dict(enumerate(INITIAL_REQUESTS, start=2))
        async with client.operation() as operation:
            sheets = await operation.call_tool("list_sheets", {"file_path": workbook})
            assert sheets.ok and sheets.data["sheets"] == ["Sheet", "Заявки"]
            cell = await operation.call_tool("read_cell", {
                "workbook_path": workbook, "sheet_name": "Sheet", "cell": "A1",
            })
            assert cell.ok and cell.data["value"] == "Сохранить"

    asyncio.run(prepare())
    monkeypatch.setattr(reset_excel, "load_excel_settings", lambda: settings)
    monkeypatch.setattr("builtins.input", lambda prompt: "СБРОС")
    assert reset_excel.main() == 0
    assert "восстановлены и проверены" in capsys.readouterr().out
    asyncio.run(verify())
