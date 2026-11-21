import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values
from mcp.types import CallToolResult, Implementation, TextContent

from config.settings import PROJECT_ROOT
from mcp_client import client as mcp_client
from mcp_client.client import McpClient, McpConnectionError, McpSession


@pytest.mark.parametrize(
    ("structured", "texts", "is_error", "status", "data"),
    [
        ({"success": True}, ['{"success": false}'], False, "success", {"success": True}),
        (None, ['{"success": false}'], False, "tool_error", {"success": False}),
        ({"error": "File not found"}, [], False, "tool_error", {"error": "File not found"}),
        ({"success": True}, [], True, "tool_error", {"success": True}),
        (None, ["MCP error"], True, "tool_error", "MCP error"),
        (None, ['{"value": "Новый"}', "message"], False, "success", [{"value": "Новый"}, "message"]),
        (None, [], False, "invalid_response", None),
    ],
)
def test_tool_result_and_journal(structured, texts, is_error, status, data):
    raw = CallToolResult(
        structured_content=structured,
        content=[TextContent(type="text", text=text) for text in texts],
        is_error=is_error,
    )
    session = AsyncMock()
    session.call_tool.return_value = raw
    operation = McpSession(session, Implementation(name="fake", version="0"), (), 1)
    arguments = {"cell": "A1"}

    result = asyncio.run(operation.call_tool("read_cell", arguments))
    arguments["cell"] = "A2"

    session.call_tool.assert_awaited_once()
    assert result.status == status
    assert result.data == data
    event, = operation.calls
    assert event.name == "read_cell"
    assert event.arguments == {"cell": "A1"}
    assert event.result == result
    assert event.duration_seconds >= 0


def test_timed_out_write_is_recorded_and_not_retried():
    async def wait_for_response(*args, **kwargs):
        await asyncio.Event().wait()

    session = AsyncMock()
    session.call_tool.side_effect = wait_for_response
    operation = McpSession(session, Implementation(name="fake", version="0"), (), 0.01)

    result = asyncio.run(operation.call_tool("write_cell", {"cell": "D2"}))

    assert result.status == "timeout"
    session.call_tool.assert_awaited_once()
    assert operation.calls[0].result.status == "timeout"


def test_connection_timeout_closes_transport_and_session(monkeypatch):
    lifecycle = []

    @asynccontextmanager
    async def transport(parameters):
        try:
            yield None, None
        finally:
            lifecycle.append("transport closed")

    @asynccontextmanager
    async def session_context(*streams):
        async def initialize():
            await asyncio.Event().wait()

        session = AsyncMock()
        session.initialize.side_effect = initialize
        try:
            yield session
        finally:
            lifecycle.append("session closed")

    monkeypatch.setattr(mcp_client, "stdio_client", transport)
    monkeypatch.setattr(mcp_client, "ClientSession", session_context)
    client = McpClient(Path("python"), connect_timeout_seconds=0.01)

    async def connect():
        async with client.operation():
            pytest.fail("Connection should time out before yielding an operation.")

    with pytest.raises(McpConnectionError, match="Истекло время"):
        asyncio.run(connect())
    assert lifecycle == ["session closed", "transport closed"]


def test_missing_interpreter_has_clear_error(tmp_path):
    client = McpClient(tmp_path / "missing.exe")

    async def connect():
        async with client.operation():
            pytest.fail("Missing interpreter should prevent a connection.")

    with pytest.raises(McpConnectionError, match="MCP_PYTHON_PATH"):
        asyncio.run(connect())


def test_real_excel_mcp_round_trip(tmp_path):
    configured_path = os.environ.get("MCP_PYTHON_PATH")
    if configured_path is None:
        configured_path = dotenv_values(PROJECT_ROOT / ".env.example")["MCP_PYTHON_PATH"]
    if not configured_path or not (PROJECT_ROOT / configured_path).is_file():
        pytest.skip("Для интеграционного теста нужен Excel MCP Server (MCP_PYTHON_PATH).")

    client = McpClient(PROJECT_ROOT / configured_path)
    file_path = str(tmp_path / "integration.xlsx")
    sheet_args = {"workbook_path": file_path, "sheet_name": "Заявки"}
    header = ["ID", "Заявитель", "Описание", "Статус"]
    first_row = [101, "Иван Петров", "Не работает принтер", "Новый"]
    last_row = [105, "Анна Смирнова", "Ноутбук тормозит", "В работе"]

    async def run_scenario():
        async with client.operation() as operation:
            assert operation.server_info.name == "Excel MCP Server"
            tools = {tool.name: tool for tool in operation.tools}
            assert {"create_workbook", "create_sheet", "read_cell", "write_cell"} <= tools.keys()
            assert "range_ref" in tools["read_range"].input_schema["required"]
            assert "start_cell" in tools["write_range"].input_schema["required"]

            assert (await operation.call_tool("create_workbook", {"file_path": file_path})).ok
            assert (await operation.call_tool("create_sheet", sheet_args)).ok
            assert (await operation.call_tool(
                "write_range", sheet_args | {"start_cell": "A1", "data": [header, first_row]}
            )).ok
            assert (await operation.call_tool(
                "write_range", sheet_args | {"start_cell": "A4", "data": [last_row]}
            )).ok
            metadata = await operation.call_tool("get_workbook_info", {"file_path": file_path})
            assert metadata.ok and "Заявки" in metadata.data["sheets"]
            rows = await operation.call_tool("read_range", sheet_args | {"range_ref": "A1:D5"})
            assert rows.ok
            assert rows.data["data"] == [header, first_row, [None] * 4, last_row, [None] * 4]
            whole_columns = await operation.call_tool(
                "read_range", sheet_args | {"range_ref": "A:D"}
            )
            assert whole_columns.status == "tool_error"
            assert (await operation.call_tool(
                "write_cell", sheet_args | {"cell": "D2", "value": "Закрыт"}
            )).ok
            verified = await operation.call_tool("read_cell", sheet_args | {"cell": "D2"})
            assert verified.ok and verified.data["value"] == "Закрыт"
            assert len(operation.calls) == 9

        with pytest.raises(RuntimeError, match="закрыта"):
            await operation.call_tool("read_cell", sheet_args | {"cell": "D2"})
        assert len(operation.calls) == 9
        async with client.operation() as verification:
            assert not verification.calls
            rows = await verification.call_tool("read_range", sheet_args | {"range_ref": "A2:D2"})
            assert rows.ok and rows.data["data"] == [first_row[:3] + ["Закрыт"]]

    asyncio.run(run_scenario())
