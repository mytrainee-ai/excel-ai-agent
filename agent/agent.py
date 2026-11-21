"""Цикл OpenAI Responses API и исполнение вызовов через контроллер."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from openai import APIError, AsyncOpenAI
from openai.types.responses import ResponseInputParam, ResponseTextConfigParam

from agent.prompts import SYSTEM_PROMPT
from agent.tool_adapter import to_function_tool
from config.settings import Settings
from mcp_client.client import McpConnectionError, ToolCall, ToolResult
from services.confirmations import confirmation_text
from services.requests_service import (
    CallLimitExceeded,
    DeniedCall,
    RejectedCall,
    RequestsController,
    RequestsError,
    RequestsOperation,
    StatusChange,
    filter_records,
)
from services.validation import STATUSES, RequestRows, TableError, normalize_range


_API_ERROR = "Не удалось выполнить запрос к OpenAI. Проверьте конфигурацию и доступ к модели."
ChatRole = Literal["user", "assistant"]
_HISTORY_MESSAGES = 8
_HISTORY_MESSAGE_CHARS = 2000
_REPLY_FORMAT: ResponseTextConfigParam = {
    "format": {
        "type": "json_schema",
        "name": "request_reply",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "filter_status": {"type": ["string", "null"], "enum": [*STATUSES, None]},
            },
            "required": ["text", "filter_status"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class AgentReply:
    text: str
    error: bool = False
    confirmation: StatusChange | None = None
    events: tuple[ToolCall | DeniedCall, ...] = ()
    records: RequestRows | None = None
    filter_status: str | None = None
    request_id: int | None = None


async def run_turn(
    settings: Settings,
    message: str,
    *,
    allow_add: bool = False,
    history: Sequence[tuple[ChatRole, str]] = (),
    last_request_id: int | None = None,
) -> AgentReply:
    """Обрабатывает сообщение и возвращает результат после закрытия MCP-сессии."""
    controller = RequestsController(settings)
    operation = None
    try:
        async with AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=30,
            max_retries=0,
        ) as client:
            async with controller.operation(allow_add=allow_add) as operation:
                conversation: ResponseInputParam = [
                    {"role": role, "content": text[:_HISTORY_MESSAGE_CHARS]}
                    for role, text in history[-_HISTORY_MESSAGES:]
                ]
                conversation.append({"role": "user", "content": message})
                instructions = SYSTEM_PROMPT
                if last_request_id is not None:
                    instructions += (
                        f"\nID последней проверенной заявки — {last_request_id}. "
                        "Учитывай его для контекстных ссылок пользователя и перечитывай "
                        "актуальные данные через MCP. При неоднозначности уточни ID."
                    )
                reply = await _respond(
                    client, settings, operation, conversation, instructions, allow_add=allow_add
                )
    except (McpConnectionError, RequestsError, TableError) as error:
        reply = AgentReply(str(error), error=True)
    except APIError:
        reply = AgentReply(_API_ERROR, error=True)
    if operation is None:
        return reply
    return replace(reply, events=operation.events, records=operation.records if not reply.error else None)


async def _respond(
    client: AsyncOpenAI,
    settings: Settings,
    operation: RequestsOperation,
    history: ResponseInputParam,
    instructions: str,
    *,
    allow_add: bool,
) -> AgentReply:
    try:
        tools = [
            to_function_tool(tool)
            for tool in operation.tools
            if allow_add or tool.name != "write_range"
        ]
    except ValueError as error:
        return AgentReply(str(error), error=True)

    handled_calls: set[str] = set()
    terminal: AgentReply | None = None
    denied_write: AgentReply | None = None
    for step in range(settings.mcp_max_tool_calls + 1):
        can_call = terminal is None and step < settings.mcp_max_tool_calls
        try:
            response = await client.responses.create(
                model=settings.openai_model,
                instructions=instructions,
                input=history,
                tools=tools,
                text=_REPLY_FORMAT,
                tool_choice="auto" if can_call else "none",
                parallel_tool_calls=False,
                store=False,
            )
        except APIError:
            return (
                terminal
                if terminal is not None and terminal.confirmation is None
                else AgentReply(_API_ERROR, error=True)
            )
        if response.status in {"failed", "incomplete", "cancelled", "in_progress", "queued"}:
            return terminal or AgentReply("OpenAI не завершил ответ. Попробуйте снова.", error=True)

        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            return terminal or denied_write or _final_reply(response.output_text, operation.records)
        if (
            not can_call
            or len(calls) != 1
            or calls[0].status not in {None, "completed"}
            or calls[0].call_id in handled_calls
        ):
            return terminal or AgentReply("Модель вернула недопустимую последовательность вызовов.", error=True)

        call = calls[0]
        handled_calls.add(call.call_id)
        try:
            arguments = json.loads(call.arguments)
        except (TypeError, ValueError):
            arguments = None

        try:
            result = await operation.call_tool(call.name, arguments)
        except RejectedCall as error:
            payload = {"status": "denied", "message": str(error)}
            if call.name in {"write_range", "write_cell"}:
                denied_write = AgentReply(str(error), error=True)
            if isinstance(error, CallLimitExceeded):
                terminal = AgentReply(str(error), error=True)
        except (RequestsError, TableError) as error:
            terminal = AgentReply(str(error), error=True)
            payload = {"status": "error", "message": str(error)}
        else:
            payload, terminal = _tool_feedback(call.name, arguments, result)

        history.extend(cast(ResponseInputParam, [
            item.to_dict(mode="json", exclude_none=True) for item in response.output
        ]))
        history.append({
            "type": "function_call_output",
            "call_id": call.call_id,
            "output": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        })
    return terminal or denied_write or AgentReply("Достигнут лимит шагов агента.", error=True)


def _final_reply(output: str, records: RequestRows | None) -> AgentReply:
    text = output.strip()
    try:
        structured = json.loads(text)
    except ValueError:
        structured = None
    if isinstance(structured, dict) and isinstance(structured.get("text"), str):
        text = structured["text"].strip()
        status = structured.get("filter_status")
        if status in STATUSES:
            if records is None:
                return AgentReply("Не удалось получить актуальные заявки для поиска.", error=True)
            matches = filter_records(records, status)
            lines = [f"Найдено заявок со статусом «{status}» — {len(matches)}."]
            lines.extend(f"№{row[0]} · {row[1]}" for row in matches)
            return AgentReply("\n".join(lines), filter_status=status)
    if text:
        return AgentReply(text)
    return AgentReply("Модель не вернула текстовый ответ.", error=True)


def _tool_feedback(
    name: str, arguments: dict[str, Any], result: ToolResult | StatusChange
) -> tuple[dict[str, Any], AgentReply | None]:
    if isinstance(result, StatusChange):
        request_id, _, description, current_status = result.original
        return {
            "status": "confirmation_required",
            "request_id": request_id,
            "description": description,
            "current_status": current_status,
            "target_status": result.target_status,
        }, AgentReply(confirmation_text(result), confirmation=result)

    data = result.data
    terminal = None
    if name == "read_range" and result.ok:
        data = _compact_range(arguments["range_ref"], data)
    if name == "write_range" and result.ok and isinstance(data, dict) and data.get("verified") is True:
        row = data["data"]
        terminal = AgentReply(f"Заявка №{row[0]} добавлена. Статус «{row[3]}».", request_id=row[0])
    elif name == "write_cell" and result.ok and isinstance(data, dict) and data.get("changed") is False:
        row = data["data"]
        terminal = AgentReply(f"У заявки №{row[0]} уже установлен статус «{row[3]}».", request_id=row[0])
    elif name in {"write_cell", "write_range"}:
        text = "Результат записи не подтверждён."
        return {"status": "error", "message": text}, AgentReply(text, error=True)
    return {"status": result.status, "data": data}, terminal


def _compact_range(range_ref: str, result: Any) -> Any:
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return result
    rows = result["data"]
    if not all(isinstance(row, list) for row in rows):
        return result
    start = int(normalize_range(range_ref).split(":")[0][1:])
    occupied = [(number, row) for number, row in enumerate(rows, start) if any(value is not None for value in row)]
    if len(occupied) == len(rows):
        return result
    return result | {
        "data": [row for _, row in occupied],
        "row_numbers": [number for number, _ in occupied],
    }
