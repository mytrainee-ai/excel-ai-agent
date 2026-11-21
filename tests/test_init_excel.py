import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values

from config.settings import PROJECT_ROOT, ExcelSettings, load_excel_settings
from mcp_client.client import McpClient, ToolResult
from scripts import init_excel


def test_excel_config_does_not_need_openai(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MCP_PYTHON_PATH=missing/python.exe\n", encoding="utf-8")
    for name in ("MCP_PYTHON_PATH", "EXCEL_FILE_PATH", "EXCEL_SHEET_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("OPENAI_MODEL", "")

    settings = load_excel_settings(env_file)

    assert settings.excel_file_path == PROJECT_ROOT / "data" / "requests.xlsx"
    assert settings.excel_sheet_name == "Заявки"


@pytest.mark.parametrize("write_status", ["success", "timeout"])
def test_failed_verification_leaves_no_destination(tmp_path, monkeypatch, write_status):
    tools = ("create_workbook", "create_sheet", "write_range", "read_range")
    operation = SimpleNamespace(
        tools=[SimpleNamespace(name=name) for name in tools],
        call_tool=AsyncMock(side_effect=[
            ToolResult("success"),
            ToolResult("success"),
            ToolResult(write_status),
            ToolResult("success", {"data": []}),
        ]),
    )

    @asynccontextmanager
    async def open_operation():
        yield operation

    monkeypatch.setattr(
        init_excel, "McpClient", lambda *args, **kwargs: SimpleNamespace(operation=open_operation)
    )
    path = tmp_path / "requests.xlsx"
    settings = ExcelSettings(path, "Заявки", Path("python"), 10, 15, 30)

    with pytest.raises(init_excel.InitializationError, match="не совпали"):
        asyncio.run(init_excel.initialize_workbook(settings))

    assert [call.args[0] for call in operation.call_tool.await_args_list] == list(tools)
    assert not path.exists()
    assert not list(tmp_path.glob(".init-*"))


def test_call_budget_is_checked_before_creating_files(tmp_path):
    path = tmp_path / "data" / "requests.xlsx"
    settings = ExcelSettings(path, "Заявки", Path("python"), 3, 15, 30)

    with pytest.raises(init_excel.InitializationError, match="MCP_MAX_TOOL_CALLS"):
        asyncio.run(init_excel.initialize_workbook(settings))

    assert not path.parent.exists()


def test_initialize_and_preserve_existing_workbook(tmp_path):
    configured_path = os.environ.get("MCP_PYTHON_PATH")
    if configured_path is None:
        configured_path = dotenv_values(PROJECT_ROOT / ".env.example")["MCP_PYTHON_PATH"]
    if not configured_path or not (PROJECT_ROOT / configured_path).is_file():
        pytest.skip("Для интеграционного теста нужен Excel MCP Server (MCP_PYTHON_PATH).")

    path = tmp_path / "data" / "requests.xlsx"
    python_path = PROJECT_ROOT / configured_path
    settings = ExcelSettings(path, "Заявки", python_path, 4, 15, 30)
    unavailable = ExcelSettings(path, "Заявки", tmp_path / "missing.exe", 1, 15, 30)

    async def scenario():
        results = await asyncio.gather(
            init_excel.initialize_workbook(settings),
            init_excel.initialize_workbook(settings),
        )
        assert sorted(results) == [False, True]
        original = path.stat()
        assert not await init_excel.initialize_workbook(unavailable)
        existing = path.stat()
        assert (existing.st_ino, existing.st_size, existing.st_mtime_ns) == (
            original.st_ino, original.st_size, original.st_mtime_ns,
        )
        assert not list(path.parent.glob(".init-*"))

        async with McpClient(python_path).operation() as operation:
            result = await operation.call_tool(
                "read_range",
                {"workbook_path": str(path), "sheet_name": "Заявки", "range_ref": "A1:D5"},
            )
            assert result.ok
            assert result.data["data"] == [
                ["ID", "Заявитель", "Описание", "Статус"],
                [101, "Иван Петров", "Не работает принтер", "Новый"],
                [102, "Елена Кузнецова", "Не удаётся подключиться к Wi-Fi", "Закрыт"],
                [103, "Мария Соколова", "Не открывается корпоративная почта", "В работе"],
                [None, None, None, None],
            ]

    asyncio.run(scenario())
