"""STDIO-клиент и журнал рабочих вызовов Excel MCP Server."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, Implementation, PaginatedRequestParams, TextContent, Tool


CallStatus = Literal[
    "success", "tool_error", "invalid_response", "timeout", "request_error", "cancelled"
]


class McpConnectionError(RuntimeError):
    """Не удалось открыть и подготовить STDIO-сессию."""


@dataclass(frozen=True)
class ToolResult:
    status: CallStatus
    data: Any = None

    @property
    def ok(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: ToolResult
    duration_seconds: float


def _decode_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _normalize(result: CallToolResult) -> ToolResult:
    payloads = (
        [result.structured_content]
        if result.structured_content is not None
        else [_decode_text(block.text) for block in result.content if isinstance(block, TextContent)]
    )
    data = payloads[0] if len(payloads) == 1 else payloads or None
    if result.is_error or any(
        isinstance(payload, dict) and (payload.get("success") is False or payload.get("error"))
        for payload in payloads
    ):
        return ToolResult("tool_error", data)
    if not payloads:
        return ToolResult("invalid_response")
    return ToolResult("success", data)


class McpSession:
    """Одна непрерывная операция и её фактические вызовы."""

    def __init__(
        self,
        session: ClientSession,
        server_info: Implementation,
        tools: tuple[Tool, ...],
        timeout_seconds: float,
    ) -> None:
        self._session: ClientSession | None = session
        self.server_info = server_info
        self.tools = tools
        self._timeout_seconds = timeout_seconds
        self._calls: list[ToolCall] = []

    @property
    def calls(self) -> tuple[ToolCall, ...]:
        return tuple(self._calls)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if self._session is None:
            raise RuntimeError("MCP-сессия уже закрыта.")
        sent_arguments = deepcopy(arguments)
        started = monotonic()
        result = ToolResult("cancelled")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                raw_result = await self._session.call_tool(name, arguments=sent_arguments)
        except TimeoutError:
            result = ToolResult("timeout")
        except Exception:
            # Ошибка запроса не гарантирует, что сервер не успел выполнить запись.
            result = ToolResult("request_error")
        else:
            result = (
                _normalize(raw_result)
                if isinstance(raw_result, CallToolResult)
                else ToolResult("invalid_response")
            )
        finally:
            self._calls.append(
                ToolCall(name, sent_arguments, deepcopy(result), monotonic() - started)
            )
        return result


class McpClient:
    """Открывает новую STDIO-сессию на каждую операцию."""

    def __init__(
        self,
        python_path: Path,
        *,
        connect_timeout_seconds: float = 30,
        tool_timeout_seconds: float = 15,
    ) -> None:
        self._parameters = StdioServerParameters(
            command=str(python_path),
            args=[
                "-B",
                "-c",
                "from excel_mcp_server import mcp; mcp.run(transport='stdio', show_banner=False)",
            ],
        )
        self._connect_timeout_seconds = connect_timeout_seconds
        self._tool_timeout_seconds = tool_timeout_seconds

    @asynccontextmanager
    async def operation(self) -> AsyncIterator[McpSession]:
        stack = AsyncExitStack()
        try:
            try:
                async with asyncio.timeout(self._connect_timeout_seconds):
                    streams = await stack.enter_async_context(stdio_client(self._parameters))
                    session = await stack.enter_async_context(ClientSession(*streams))
                    info = await session.initialize()
                    page = await session.list_tools()
                    tools = list(page.tools)
                    while page.next_cursor is not None:
                        page = await session.list_tools(
                            params=PaginatedRequestParams(cursor=page.next_cursor)
                        )
                        tools.extend(page.tools)
            except TimeoutError as error:
                raise McpConnectionError("Истекло время подключения к Excel MCP Server.") from error
            except Exception as error:
                raise McpConnectionError(
                    "Не удалось подключиться к Excel MCP Server. Проверьте MCP_PYTHON_PATH "
                    "и наличие excel_mcp_server в окружении сервера."
                ) from error

            operation = McpSession(session, info.server_info, tuple(tools), self._tool_timeout_seconds)
            try:
                yield operation
            finally:
                operation._session = None
        finally:
            await stack.aclose()
