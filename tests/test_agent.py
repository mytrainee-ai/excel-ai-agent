import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from dotenv import dotenv_values
from mcp.types import CallToolResult, Implementation, Tool
from openai import AsyncOpenAI

from agent import agent
from agent.tool_adapter import to_function_tool
from config.settings import PROJECT_ROOT, Settings
from mcp_client.client import McpSession
from scripts.init_excel import initialize_workbook
from services.requests_service import DeniedCall, RequestsController, RequestsOperation
from services.validation import HEADERS, LAST_ROW


def _settings(path, *, budget=10, python_path=Path("python")):
    return Settings(
        excel_file_path=path,
        excel_sheet_name="Заявки",
        mcp_python_path=python_path,
        mcp_max_tool_calls=budget,
        mcp_tool_timeout_seconds=15,
        mcp_connect_timeout_seconds=30,
        openai_api_key="test-api-key",
        openai_base_url="https://example.invalid/v1",
        openai_model="test-model",
    )


def _tool(name, argument):
    return Tool(
        name=name,
        description="Схема MCP",
        input_schema={
            "type": "object",
            "properties": {key: {"type": "string"} for key in ("workbook_path", "sheet_name", argument)},
            "required": ["workbook_path", "sheet_name", argument],
        },
    )


def _call(name, call_id, arguments):
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(arguments),
    }


def _message(text):
    return {
        "id": "message1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


@pytest.fixture
def mock_openai(monkeypatch):
    def start(*responses):
        requests = []

        def handle(request):
            assert request.url.path == "/v1/responses"
            requests.append(json.loads(request.content))
            assert "test-api-key" not in request.content.decode()
            response = responses[len(requests) - 1]
            if isinstance(response, int):
                return httpx2.Response(response, json={"error": {"message": "internal detail"}})
            return httpx2.Response(200, json={
                "id": f"resp_{len(requests)}",
                "object": "response",
                "status": "completed",
                "output": response,
            })

        def client(**options):
            return AsyncOpenAI(
                **options, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
            )

        monkeypatch.setattr(agent, "AsyncOpenAI", client)
        return requests
    return start


@pytest.fixture
def mock_mcp(monkeypatch):
    def start(*results):
        sdk = AsyncMock()
        sdk.call_tool.side_effect = results
        tools = (_tool("read_range", "range_ref"), _tool("read_cell", "cell"))

        def controller(settings):
            @asynccontextmanager
            async def operation(*, allow_add):
                session = McpSession(sdk, Implementation(name="MCP", version="1"), tools, 1)
                yield RequestsOperation(session, settings, allow_add=allow_add)
            return SimpleNamespace(operation=operation)

        monkeypatch.setattr(agent, "RequestsController", controller)
        return sdk
    return start


def _range(rows):
    return CallToolResult(content=[], structured_content={"success": True, "data": rows})


def _table(*records):
    rows = [[None] * 4 for _ in range(LAST_ROW + 1)]
    rows[0] = list(HEADERS)
    rows[1:1 + len(records)] = records
    return _range(rows)


def _feedback(request):
    output = request["input"][-1]
    assert output["type"] == "function_call_output"
    return output["call_id"], json.loads(output["output"])


def test_adapts_mcp_schema_without_mutating_it():
    source = _tool("read_range", "range_ref")
    schema = to_function_tool(source)

    assert schema["name"] == source.name
    assert schema["description"] == source.description
    assert schema["strict"] is False
    assert schema["parameters"]["properties"] == {"range_ref": {"type": "string"}}
    assert schema["parameters"]["required"] == ["range_ref"]
    assert "workbook_path" in source.input_schema["properties"]


def test_agent_relays_reasoning_and_real_result_after_denial(tmp_path, mock_openai, mock_mcp):
    rows = [["ID", "Заявитель", "Описание", "Статус"], [None] * 4, [103, "Мария", "Почта", "В работе"]]
    sdk = mock_mcp(_table(), _range(rows))
    reasoning = {"type": "reasoning", "id": "rs1", "summary": [], "encrypted_content": "mock-reasoning"}
    requests = mock_openai(
        [_call("delete_sheet", "c1", {})],
        [reasoning, _call("read_range", "c2", {"range_ref": "A1:D3"})],
        [_message("Найдена одна заявка №103.")],
    )

    reply = asyncio.run(agent.run_turn(_settings(tmp_path / "requests.xlsx"), "Что в работе?"))

    assert reply.text == "Найдена одна заявка №103."
    assert sdk.call_tool.await_count == 2
    assert [event.name for event in reply.events] == ["delete_sheet", "read_range", "read_range"]
    assert isinstance(reply.events[0], DeniedCall)
    assert set(tool["name"] for tool in requests[0]["tools"]) == {"read_range", "read_cell"}
    assert requests[0]["model"] == "test-model"
    assert requests[0]["parallel_tool_calls"] is False and requests[0]["store"] is False
    assert _feedback(requests[1])[1]["status"] == "denied"
    assert reasoning in requests[2]["input"]
    call_id, payload = _feedback(requests[2])
    assert call_id == "c2"
    assert payload["data"]["data"] == [rows[0], rows[2]]
    assert payload["data"]["row_numbers"] == [1, 3]


def test_openai_failure_retains_mcp_events_and_hides_error_body(tmp_path, mock_openai, mock_mcp):
    mock_mcp(_table(), _range([list(HEADERS)]))
    requests = mock_openai([_call("read_range", "c1", {"range_ref": "A1:D1"})], 401)

    reply = asyncio.run(agent.run_turn(_settings(tmp_path / "requests.xlsx"), "Покажи заголовки."))

    assert reply.error and "OpenAI" in reply.text
    assert "internal detail" not in reply.text
    assert len(reply.events) == 2
    assert len(requests) == 2


def test_structured_status_filter_uses_verified_table(tmp_path, mock_openai, mock_mcp):
    row = [103, "Мария", "Почта", "В работе"]
    mock_mcp(_table(row), _range([list(HEADERS), row]))
    requests = mock_openai(
        [_call("read_range", "c1", {"range_ref": "A1:D2"})],
        [_message(json.dumps({"text": "Найдено 99 заявок.", "filter_status": "В работе"}))],
    )

    reply = asyncio.run(agent.run_turn(_settings(tmp_path / "requests.xlsx"), "Покажи все заявки в работе."))

    assert reply.filter_status == "В работе"
    assert reply.records == {2: tuple(row)}
    assert "— 1." in reply.text and "№103" in reply.text and "99" not in reply.text
    assert requests[0]["text"]["format"]["type"] == "json_schema"


def test_filter_requires_fresh_mcp_data(tmp_path, mock_openai, mock_mcp):
    sdk = mock_mcp()
    mock_openai([_message(json.dumps({"text": "Найдены заявки.", "filter_status": "В работе"}))])

    reply = asyncio.run(agent.run_turn(_settings(tmp_path / "requests.xlsx"), "Покажи все заявки в работе."))

    assert reply.error
    assert reply.filter_status is None
    sdk.call_tool.assert_not_awaited()


def test_agent_stops_at_controller_limit(tmp_path, mock_openai, mock_mcp):
    sdk = mock_mcp()
    requests = mock_openai(
        [_call("read_range", "c1", {"range_ref": "A1:D1"})],
        [_message("Инструмент вызван.")],
    )

    reply = asyncio.run(agent.run_turn(_settings(tmp_path / "requests.xlsx", budget=1), "Читай."))

    assert reply.error and "Лимит" in reply.text
    assert requests[-1]["tool_choice"] == "none"
    assert _feedback(requests[-1])[1]["status"] == "denied"
    sdk.call_tool.assert_not_awaited()


def test_agent_does_not_execute_duplicate_call_id(tmp_path, mock_openai, mock_mcp):
    sdk = mock_mcp(_table(), _range([list(HEADERS)]))
    call = _call("read_range", "c1", {"range_ref": "A1:D1"})
    mock_openai([call], [call])

    reply = asyncio.run(agent.run_turn(_settings(tmp_path / "requests.xlsx"), "Покажи заголовки."))

    assert reply.error
    assert sdk.call_tool.await_count == 2


def test_agent_sends_short_dialogue_context_without_mcp_log(tmp_path, mock_openai, mock_mcp):
    mock_mcp()
    requests = mock_openai([_message("На какой статус изменить заявку?")])
    messages = [("user", f"Запрос {number}") for number in range(10)]
    messages.append(("assistant", "Заявка №104 добавлена."))

    reply = asyncio.run(agent.run_turn(
        _settings(tmp_path / "requests.xlsx"),
        "Измени её статус.",
        history=messages,
        last_request_id=104,
    ))

    assert not reply.error
    assert "ID последней проверенной заявки — 104" in requests[0]["instructions"]
    assert requests[0]["input"][0] == {"role": "user", "content": "Запрос 3"}
    assert requests[0]["input"][-2] == {"role": "assistant", "content": "Заявка №104 добавлена."}
    assert requests[0]["input"][-1] == {"role": "user", "content": "Измени её статус."}


def test_real_mcp_uses_live_schemas_and_returns_verified_write(tmp_path, mock_openai):
    interpreter = os.environ.get("MCP_PYTHON_PATH")
    if interpreter is None:
        interpreter = dotenv_values(PROJECT_ROOT / ".env.example")["MCP_PYTHON_PATH"]
    if not interpreter or not (PROJECT_ROOT / interpreter).is_file():
        pytest.skip("Для интеграционного теста нужен Excel MCP Server.")
    settings = _settings(tmp_path / "requests.xlsx", python_path=PROJECT_ROOT / interpreter)
    row = [104, "Анна Смирнова", "Ноутбук тормозит", "Новый"]
    requests = mock_openai(
        [_call("read_range", "c1", {"range_ref": f"A1:D{LAST_ROW}"})],
        [_call("write_range", "c2", {"start_cell": "A5", "data": [row]})],
        [_message("Добавлены заявки №104 и №105.")],
        [_call("write_cell", "c3", {"cell": "D5", "value": "В работе"})],
        [_message("Статус уже изменён.")],
    )

    async def scenario():
        assert await initialize_workbook(settings)
        reply = await agent.run_turn(settings, "Добавь заявку Анны Смирновой. Ноутбук тормозит.", allow_add=True)
        assert not reply.error and "104" in reply.text and "105" not in reply.text
        assert reply.records[5] == tuple(row)
        reply = await agent.run_turn(settings, "Измени статус заявки 104 на «В работе».")
        assert reply.confirmation is not None and "Подтвердите" in reply.text
        assert [event.name for event in reply.events] == ["read_range"]
        async with RequestsController(settings).operation() as operation:
            assert (await operation.read_table())[5] == tuple(row)

    asyncio.run(scenario())
    schemas = {tool["name"]: tool["parameters"] for tool in requests[0]["tools"]}
    assert set(schemas) == {"read_range", "read_cell", "write_range", "write_cell"}
    assert set(schemas["write_range"]["required"]) == {"start_cell", "data"}
    assert requests[2]["tool_choice"] == "none"
    assert _feedback(requests[2])[1]["data"]["verified"] is True
    assert _feedback(requests[4])[1]["status"] == "confirmation_required"
