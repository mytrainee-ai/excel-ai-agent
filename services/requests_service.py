"""Контроллер доступа к учебной книге заявок."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from config.settings import ExcelSettings
from mcp_client.client import McpClient, McpSession, ToolCall, ToolResult
from services.validation import (
    LAST_ROW,
    MAX_DATA_ROWS,
    STATUSES,
    TABLE_RANGE,
    RangeError,
    RequestRows,
    is_request_text,
    normalize_cell,
    normalize_range,
    parse_table,
)


_TOOL_ARGUMENTS = {
    "read_cell": {"cell"},
    "read_range": {"range_ref"},
    "write_cell": {"cell", "value"},
    "write_range": {"start_cell", "data"},
}
_MUTATION_LOCK = Lock()


class RequestsError(RuntimeError):
    """Операцию с заявками не удалось завершить."""


class RejectedCall(RequestsError):
    """Контроллер отклонил вызов до обращения к MCP."""


class CallLimitExceeded(RejectedCall):
    """В операции недостаточно рабочих MCP-вызовов."""


@dataclass(frozen=True)
class DeniedCall:
    name: str
    reason: str


def filter_records(records: RequestRows, status: str | None) -> list[tuple[Any, ...]]:
    return [row for _, row in sorted(records.items()) if status is None or row[3] == status]


@dataclass
class _SessionScope:
    closed: bool = False
    confirmation_used: bool = False


@dataclass(frozen=True)
class StatusChange:
    """Снимок заявки для показа пользователю перед подтверждением."""

    original: tuple[Any, ...]
    target_status: str
    _workbook: Path = field(repr=False)
    _sheet: str = field(repr=False)
    _scope: _SessionScope = field(repr=False, compare=False)

    @property
    def request_id(self) -> int:
        return self.original[0]


class StaleConfirmation(RejectedCall):
    def __init__(self, replacement: StatusChange) -> None:
        super().__init__("Данные заявки изменились. Проверьте их и подтвердите новое предложение.")
        self.replacement = replacement


@contextmanager
def _exclusive_mutation() -> Iterator[None]:
    if not _MUTATION_LOCK.acquire(blocking=False):
        raise RejectedCall("Другая операция уже записывает данные. Повторите запрос после её завершения.")
    try:
        yield
    finally:
        _MUTATION_LOCK.release()


class RequestsOperation:
    """Проверяет вызовы модели в пределах одной MCP-сессии."""

    def __init__(self, session: McpSession, settings: ExcelSettings, *, allow_add: bool) -> None:
        self._session = session
        self._settings = settings
        self._allow_add = allow_add
        self._scope = _SessionScope()
        self._gate = asyncio.Lock()
        self._records: RequestRows | None = None
        self._add_attempted = False
        self._pending_status = False
        self._events: list[ToolCall | DeniedCall] = []
        self.tools = tuple(tool for tool in session.tools if tool.name in _TOOL_ARGUMENTS)
        self._tool_names = {tool.name for tool in self.tools}

    @property
    def events(self) -> tuple[ToolCall | DeniedCall, ...]:
        return tuple(self._events)

    @property
    def records(self) -> RequestRows | None:
        return self._records.copy() if self._records is not None else None

    def _require_calls(self, count: int) -> None:
        if self._scope.closed:
            raise RejectedCall("Операция уже завершена.")
        if len(self._session.calls) + count > self._settings.mcp_max_tool_calls:
            raise CallLimitExceeded("Лимит MCP-вызовов исчерпан или не хватает вызовов для проверки записи.")

    async def _call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self._require_calls(1)
        if name not in self._tool_names:
            raise RejectedCall("Этот MCP-инструмент недоступен в текущей сессии.")
        before = len(self._session.calls)
        try:
            return await self._session.call_tool(
                name,
                arguments | {
                    "workbook_path": str(self._settings.excel_file_path),
                    "sheet_name": self._settings.excel_sheet_name,
                },
            )
        finally:
            self._events.extend(self._session.calls[before:])

    def _tool_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tool_names:
            raise RejectedCall("Этот MCP-инструмент запрещён или недоступен.")
        expected = _TOOL_ARGUMENTS[name]
        if not isinstance(arguments, dict) or not expected <= arguments.keys() <= (
            expected | {"workbook_path", "sheet_name"}
        ):
            raise RejectedCall("Передайте только обязательные аргументы инструмента.")
        path = str(self._settings.excel_file_path)
        if arguments.get("workbook_path", path) != path:
            raise RejectedCall("Доступ разрешён только к настроенной книге заявок.")
        sheet = self._settings.excel_sheet_name
        if arguments.get("sheet_name", sheet) != sheet:
            raise RejectedCall("Доступ разрешён только к настроенному листу заявок.")
        return {key: arguments[key] for key in expected}

    async def _read_table(self) -> RequestRows:
        self._records = None
        result = await self._call("read_range", {"range_ref": TABLE_RANGE})
        if not result.ok:
            raise RequestsError(f"Не удалось прочитать настроенный лист заявок ({result.status}).")
        data = result.data.get("data") if isinstance(result.data, dict) else None
        self._records = parse_table(data)
        return self._records.copy()

    async def read_table(self) -> RequestRows:
        """Читает и валидирует актуальную таблицу для обновления интерфейса."""
        async with self._gate:
            return await self._read_table()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult | StatusChange:
        async with self._gate:
            try:
                args = self._tool_arguments(name, arguments)
                if name == "write_range":
                    return await self._add_request(args)
                if name == "write_cell":
                    return await self._prepare_status(args)
                key = "cell" if name == "read_cell" else "range_ref"
                args[key] = normalize_cell(args[key]) if key == "cell" else normalize_range(args[key])
                self._require_calls(1 if self._records is not None else 2)
                if self._records is None:
                    await self._read_table()
                return await self._call(name, args)
            except RejectedCall as error:
                self._events.append(DeniedCall(name, str(error)))
                raise
            except RangeError as error:
                self._events.append(DeniedCall(name, str(error)))
                raise RejectedCall(str(error)) from error

    async def _add_request(self, args: dict[str, Any]) -> ToolResult:
        if not self._allow_add or self._add_attempted:
            raise RejectedCall("Добавление не разрешено или уже выполнялось в этой операции.")
        start = normalize_cell(args["start_cell"])
        number = int(start[1:])
        if start[0] != "A" or number == 1:
            raise RejectedCall("Новая заявка должна занимать одну строку данных в столбцах A–D.")
        data = args["data"]
        if (
            not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], list)
            or len(data[0]) != 4
        ):
            raise RejectedCall("Передайте одну строку с ID, заявителем, описанием и статусом.")
        request_id, requester, description, status = data[0]
        if type(request_id) is not int or not is_request_text(requester) or not is_request_text(description):
            raise RejectedCall("Укажите целый ID, заявителя и описание обычным текстом.")
        if status != STATUSES[0]:
            raise RejectedCall("Начальный статус заявки должен быть «Новый».")

        self._require_calls(3)
        with _exclusive_mutation():
            records = await self._read_table()
            if len(records) == MAX_DATA_ROWS:
                raise RejectedCall("Учебная таблица заполнена.")
            if number in records:
                raise RejectedCall("Целевая строка уже занята.")
            next_id = max((row[0] for row in records.values()), default=100) + 1
            if request_id != next_id:
                raise RejectedCall(f"Следующий ID по актуальной таблице — {next_id}.")
            self._add_attempted = True
            return await self._verified_write(
                "write_range", {"start_cell": start, "data": data}, number, tuple(data[0])
            )

    async def _prepare_status(self, args: dict[str, Any]) -> ToolResult | StatusChange:
        cell = normalize_cell(args["cell"])
        number = int(cell[1:])
        target = args["value"]
        if cell[0] != "D" or number == 1 or target not in STATUSES:
            raise RejectedCall("Обновлять можно только статус существующей заявки в столбце D.")
        if self._pending_status:
            raise RejectedCall("В этой операции уже подготовлено изменение статуса.")
        records = await self._read_table()
        original = records.get(number)
        if original is None:
            raise RejectedCall("В целевой строке нет заявки.")
        if original[3] == target:
            return ToolResult("success", {"changed": False, "data": list(original)})

        self._pending_status = True
        return self._status_change(original, target)

    def _status_change(self, original: tuple[Any, ...], target: str) -> StatusChange:
        return StatusChange(
            original, target, self._settings.excel_file_path, self._settings.excel_sheet_name, self._scope
        )

    async def apply_confirmation(self, change: StatusChange, *, confirmed: bool) -> ToolResult:
        """Вход для обработчика явного подтверждения в новой сессии."""
        async with self._gate:
            try:
                return await self._apply_status(change, confirmed)
            except RejectedCall as error:
                self._events.append(DeniedCall("write_cell", str(error)))
                raise

    async def _apply_status(self, change: StatusChange, confirmed: bool) -> ToolResult:
        if not isinstance(change, StatusChange) or confirmed is not True or not change._scope.closed:
            raise RejectedCall("Требуется явное подтверждение после закрытия исходной сессии.")
        if change.target_status not in STATUSES:
            raise RejectedCall("Подтверждение содержит недопустимый статус.")
        if (change._workbook, change._sheet) != (
            self._settings.excel_file_path, self._settings.excel_sheet_name
        ):
            raise RejectedCall("Подтверждение относится к другой книге или листу.")

        self._require_calls(3)
        with _exclusive_mutation():
            if change._scope.confirmation_used:
                raise RejectedCall("Это подтверждение уже использовано.")
            change._scope.confirmation_used = True
            records = await self._read_table()
            number = next((number for number, row in records.items() if row[0] == change.request_id), None)
            if number is None:
                raise RejectedCall("Заявка отсутствует. Запись отменена.")
            current = records[number]
            if current[3] == change.target_status:
                return ToolResult("success", {"changed": False, "data": list(current)})
            if current != change.original:
                raise StaleConfirmation(self._status_change(current, change.target_status))
            return await self._verified_write(
                "write_cell",
                {"cell": f"D{number}", "value": change.target_status},
                number,
                (*change.original[:3], change.target_status),
            )

    async def _verified_write(
        self, name: str, args: dict[str, Any], number: int, expected: tuple[Any, ...]
    ) -> ToolResult:
        records = self._records
        self._records = None
        write = await self._call(name, args)
        if write.status == "tool_error":
            raise RequestsError(f"MCP сообщил об ошибке записи ({name}).")

        read = await self._call("read_range", {"range_ref": f"A{number}:D{number}"})
        data = read.data.get("data") if isinstance(read.data, dict) else None
        if read.ok and data == [list(expected)] and type(data[0][0]) is int:
            if records is not None:
                records[number] = tuple(data[0])
                self._records = records
            return ToolResult("success", {"changed": True, "verified": True, "data": data[0]})
        if not write.ok and not read.ok:
            raise RequestsError("Результат записи неизвестен, повторное чтение не удалось.")
        raise RequestsError("Контрольное чтение не подтвердило ожидаемый результат записи.")


class RequestsController:
    def __init__(self, settings: ExcelSettings) -> None:
        self._settings = settings
        self._client = McpClient(
            settings.mcp_python_path,
            connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
            tool_timeout_seconds=settings.mcp_tool_timeout_seconds,
        )

    @asynccontextmanager
    async def operation(self, *, allow_add: bool = False) -> AsyncIterator[RequestsOperation]:
        if not self._settings.excel_file_path.is_file():
            raise RequestsError("Книга заявок отсутствует. Запустите python -m scripts.init_excel.")
        operation = None
        try:
            async with self._client.operation() as session:
                operation = RequestsOperation(session, self._settings, allow_add=allow_add)
                yield operation
        finally:
            if operation is not None:
                operation._scope.closed = True
