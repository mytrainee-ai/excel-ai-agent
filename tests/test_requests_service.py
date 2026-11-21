import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values
from mcp.types import CallToolResult, Implementation, Tool

from config.settings import PROJECT_ROOT, ExcelSettings
from mcp_client.client import McpSession
from scripts.init_excel import initialize_workbook
from services.requests_service import (
    CallLimitExceeded,
    DeniedCall,
    RejectedCall,
    RequestsController,
    RequestsError,
    RequestsOperation,
    StatusChange,
)
from services.validation import HEADERS, LAST_ROW, TABLE_RANGE


@pytest.fixture
def fake_operation(tmp_path):
    def create(*, allow_add=False, budget=10):
        sdk = AsyncMock()
        tools = tuple(
            Tool(name=name, input_schema={"type": "object"})
            for name in ("read_range", "read_cell", "write_cell", "write_range", "delete_sheet")
        )
        session = McpSession(sdk, Implementation(name="fake", version="0"), tools, 1)
        settings = ExcelSettings(tmp_path / "requests.xlsx", "Заявки", Path("python"), budget, 15, 30)
        return RequestsOperation(session, settings, allow_add=allow_add), sdk, settings
    return create


def table(*occupied):
    rows = [[None] * 4 for _ in range(LAST_ROW + 1)]
    rows[0] = list(HEADERS)
    for number, data in occupied:
        rows[number - 1] = data
    return mcp_result(data=rows)


def mcp_result(**data):
    return CallToolResult(content=[], structured_content={"success": True, **data})


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("delete_sheet", {}),
        ("read_cell", {"cell": "E2"}),
        ("read_cell", {"cell": "A1002"}),
        ("read_cell", {"cell": 2}),
        ("read_cell", {"cell": "A1", "workbook_path": "another.xlsx"}),
        ("read_cell", {"cell": "A1", "sheet_name": "Sheet"}),
        ("read_range", {"range_ref": "D5:A1"}),
        ("read_range", {}),
        ("write_cell", {"cell": "D2", "value": "Закрыт", "confirmed": True}),
        ("write_cell", {"cell": "B2", "value": "Закрыт"}),
        ("write_cell", {"cell": "D1", "value": "Закрыт"}),
        ("write_cell", {"cell": "D2", "value": "Удалён"}),
        ("write_range", {"start_cell": "A1", "data": [[101, "Анна", "Ноутбук", "Новый"]]}),
        ("write_range", {"start_cell": "A2", "data": [[101, "Анна", "=1+1", "Новый"]]}),
        ("write_range", {"start_cell": "A2", "data": [[101, "Анна", "Ноутбук", "Закрыт"]]}),
    ],
)
def test_rejected_call_never_reaches_mcp(fake_operation, name, args):
    operation, sdk, _ = fake_operation(allow_add=True)

    with pytest.raises(RejectedCall):
        asyncio.run(operation.call_tool(name, args))

    assert {tool.name for tool in operation.tools} == {"read_cell", "read_range", "write_cell", "write_range"}
    sdk.call_tool.assert_not_awaited()
    assert isinstance(operation.events[0], DeniedCall)


def test_validated_read_injects_config_and_respects_budget(fake_operation):
    operation, sdk, settings = fake_operation(budget=2)
    sdk.call_tool.side_effect = [table(), mcp_result(value="ID")]

    async def scenario():
        response = await operation.call_tool("read_cell", {"cell": "a1"})
        assert response.ok and response.data["value"] == "ID"
        with pytest.raises(CallLimitExceeded):
            await operation.call_tool("read_cell", {"cell": "A2"})

    asyncio.run(scenario())
    assert sdk.call_tool.await_count == 2
    assert operation.events[0].arguments["range_ref"] == TABLE_RANGE
    assert operation.events[1].arguments == {
        "cell": "A1", "workbook_path": str(settings.excel_file_path), "sheet_name": "Заявки"
    }
    assert isinstance(operation.events[2], DeniedCall)


@pytest.mark.parametrize("allow_add,budget", [(False, 10), (True, 2)])
def test_write_requires_permission_and_verification_budget(fake_operation, allow_add, budget):
    operation, sdk, _ = fake_operation(allow_add=allow_add, budget=budget)
    arguments = {"start_cell": "A2", "data": [[101, "Анна", "Ноутбук", "Новый"]]}
    with pytest.raises(RejectedCall):
        asyncio.run(operation.call_tool("write_range", arguments))
    sdk.call_tool.assert_not_awaited()


def test_add_reads_all_rows_and_reconciles_unknown_write(fake_operation):
    operation, sdk, _ = fake_operation(allow_add=True)
    existing = table((LAST_ROW, [150, "Иван", "Принтер", "Новый"]))
    new_row = [151, "Анна", "Ноутбук", "Новый"]
    sdk.call_tool.side_effect = [
        existing,
        OSError("disconnected before response"),
        mcp_result(data=[new_row]),
    ]
    arguments = {"start_cell": "A2", "data": [new_row]}

    async def scenario():
        result = await operation.call_tool("write_range", arguments)
        assert result.ok and result.data["verified"]
        with pytest.raises(RejectedCall, match="уже выполнялось"):
            await operation.call_tool("write_range", arguments)

    asyncio.run(scenario())
    assert [event.name for event in operation.events] == [
        "read_range", "write_range", "read_range", "write_range",
    ]
    assert operation.events[1].result.status == "request_error"
    assert operation.events[2].arguments["range_ref"] == "A2:D2"
    assert isinstance(operation.events[3], DeniedCall)


def test_add_is_blocked_when_all_thousand_rows_are_occupied(fake_operation):
    operation, sdk, _ = fake_operation(allow_add=True)
    sdk.call_tool.return_value = table(
        *((row, [row, "Иван", "Принтер", "Новый"]) for row in range(2, LAST_ROW + 1))
    )
    with pytest.raises(RejectedCall, match="заполнена"):
        asyncio.run(operation.call_tool(
            "write_range", {"start_cell": "A2", "data": [[1002, "Анна", "Ноутбук", "Новый"]]}
        ))
    assert sdk.call_tool.await_count == 1


@pytest.mark.parametrize(
    ("cell", "request_id", "reason"),
    [("A2", 106, "строка уже занята"), ("A3", 102, "Следующий ID")],
)
def test_add_rejects_a_stale_target_or_id(fake_operation, cell, request_id, reason):
    operation, sdk, _ = fake_operation(allow_add=True)
    sdk.call_tool.return_value = table(
        (2, [101, "Иван", "Принтер", "Новый"]),
        (5, [105, "Мария", "Почта", "В работе"]),
    )

    with pytest.raises(RejectedCall, match=reason):
        asyncio.run(operation.call_tool(
            "write_range", {"start_cell": cell, "data": [[request_id, "Анна", "Ноутбук", "Новый"]]}
        ))

    assert sdk.call_tool.await_count == 1


def test_add_never_retries_when_verification_is_unavailable(fake_operation):
    operation, sdk, _ = fake_operation(allow_add=True)
    sdk.call_tool.side_effect = [table(), OSError("write response lost"), OSError("read unavailable")]
    arguments = {"start_cell": "A2", "data": [[101, "Анна", "Ноутбук", "Новый"]]}

    async def scenario():
        with pytest.raises(RequestsError, match="Результат записи неизвестен"):
            await operation.call_tool("write_range", arguments)
        with pytest.raises(RejectedCall, match="уже выполнялось"):
            await operation.call_tool("write_range", arguments)

    asyncio.run(scenario())
    assert sdk.call_tool.await_count == 3


def test_status_change_checks_confirmation_and_current_values(fake_operation):
    prepare, prepare_sdk, _ = fake_operation()
    confirm, confirm_sdk, _ = fake_operation()
    prepare_sdk.call_tool.return_value = table((2, [101, "Иван", "Принтер", "Новый"]))
    confirm_sdk.call_tool.return_value = table((2, [101, "Иван", "Другие данные", "Новый"]))

    async def scenario():
        change = await prepare.call_tool("write_cell", {"cell": "D2", "value": "Закрыт"})
        assert isinstance(change, StatusChange)
        with pytest.raises(RejectedCall, match="закрытия исходной сессии"):
            await confirm.apply_confirmation(change, confirmed=True)
        prepare._scope.closed = True
        with pytest.raises(RejectedCall, match="явное подтверждение"):
            await confirm.apply_confirmation(change, confirmed=False)
        confirm_sdk.call_tool.assert_not_awaited()
        with pytest.raises(RejectedCall, match="Данные заявки изменились"):
            await confirm.apply_confirmation(change, confirmed=True)

    asyncio.run(scenario())
    assert prepare_sdk.call_tool.await_count == 1
    assert confirm_sdk.call_tool.await_count == 1
    assert all(event.name == "read_range" for event in confirm.events if not isinstance(event, DeniedCall))


def test_real_controller_adds_and_verifies_confirmed_status(tmp_path):
    configured_path = os.environ.get("MCP_PYTHON_PATH")
    if configured_path is None:
        configured_path = dotenv_values(PROJECT_ROOT / ".env.example")["MCP_PYTHON_PATH"]
    if not configured_path or not (PROJECT_ROOT / configured_path).is_file():
        pytest.skip("Для интеграционного теста нужен Excel MCP Server (MCP_PYTHON_PATH).")
    settings = ExcelSettings(
        tmp_path / "requests.xlsx", "Заявки", PROJECT_ROOT / configured_path, 10, 15, 30
    )

    async def scenario():
        assert await initialize_workbook(settings)
        controller = RequestsController(settings)
        row = [104, "Анна Смирнова", "ноутбук сильно тормозит", "Новый"]
        async with controller.operation(allow_add=True) as operation:
            result = await operation.call_tool("write_range", {"start_cell": "A5", "data": [row]})
            assert result.ok and result.data["data"] == row
            change = await operation.call_tool("write_cell", {"cell": "D5", "value": "В работе"})
            assert isinstance(change, StatusChange) and change.request_id == 104
            assert all(event.name != "write_cell" for event in operation.events)

        async with controller.operation() as operation:
            result = await operation.apply_confirmation(change, confirmed=True)
            assert result.data["data"] == [*row[:3], "В работе"]
            with pytest.raises(RejectedCall, match="уже использовано"):
                await operation.apply_confirmation(change, confirmed=True)
            records = await operation.read_table()
            assert records[5] == (*row[:3], "В работе")

    asyncio.run(scenario())
