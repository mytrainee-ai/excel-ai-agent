"""Возвращает учебный лист к исходным трём заявкам после подтверждения."""

import asyncio
import sys
from typing import Any
from uuid import uuid4

from config.settings import ExcelSettings, SettingsError, load_excel_settings
from mcp_client.client import McpClient, McpConnectionError, McpSession, ToolResult
from scripts.init_excel import INITIAL_REQUESTS
from services.validation import HEADERS, TABLE_RANGE, TableError, parse_table


_REQUIRED_TOOLS = {
    "list_sheets", "create_sheet", "rename_sheet", "delete_sheet", "write_range", "read_range",
}


class ResetError(RuntimeError):
    """Сброс книги остановлен."""


class _SheetReset:
    def __init__(self, operation: McpSession, settings: ExcelSettings) -> None:
        self.operation = operation
        self.path = str(settings.excel_file_path)
        self.limit = settings.mcp_max_tool_calls

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if len(self.operation.calls) >= self.limit:
            raise ResetError("Исчерпан лимит MCP-вызовов при сбросе.")
        return await self.operation.call_tool(name, arguments)

    async def sheets(self) -> list[str]:
        result = await self.call("list_sheets", {"file_path": self.path})
        sheets = result.data.get("sheets") if isinstance(result.data, dict) else None
        if not result.ok or not isinstance(sheets, list) or not all(isinstance(name, str) for name in sheets):
            raise ResetError(f"Не удалось проверить список листов ({result.status}).")
        return sheets

    async def rename(self, source: str, destination: str) -> None:
        result = await self.call(
            "rename_sheet", {"workbook_path": self.path, "old_name": source, "new_name": destination}
        )
        if not result.ok:
            sheets = await self.sheets()
            if source in sheets or destination not in sheets:
                raise ResetError(f"Не удалось подтвердить переименование «{source}» в «{destination}».")

    async def verify_seed(self, name: str) -> None:
        result = await self.call("read_range", {
            "workbook_path": self.path, "sheet_name": name, "range_ref": TABLE_RANGE,
        })
        if not result.ok or not isinstance(result.data, dict):
            raise ResetError(f"Не удалось перечитать лист «{name}» ({result.status}).")
        records = parse_table(result.data.get("data"))
        if records != dict(enumerate(INITIAL_REQUESTS, start=2)):
            raise ResetError(f"Содержимое листа «{name}» не совпало с исходными данными.")

    async def run(self, target: str) -> None:
        original = await self.sheets()
        has_target = target in original
        if self.limit < (9 if has_target else 7):
            raise ResetError("MCP_MAX_TOOL_CALLS недостаточен для сброса и контрольного чтения.")

        suffix = uuid4().hex[:12]
        staged, backup = f"__reset_new_{suffix}", f"__reset_old_{suffix}"
        index = original.index(target) if has_target else len(original)
        staged_args = {"workbook_path": self.path, "sheet_name": staged}
        try:
            created = await self.call("create_sheet", staged_args | {"index": index})
            if not created.ok and staged not in await self.sheets():
                raise ResetError("Не удалось подтвердить создание временного листа.")

            rows = [list(row) for row in (HEADERS, *INITIAL_REQUESTS)]
            written = await self.call("write_range", staged_args | {"start_cell": "A1", "data": rows})
            if written.status == "tool_error":
                raise ResetError("MCP сообщил об ошибке записи на временный лист.")
            await self.verify_seed(staged)

            if has_target:
                await self.rename(target, backup)
            await self.rename(staged, target)
            await self.verify_seed(target)

            if has_target:
                await self.call("delete_sheet", {"workbook_path": self.path, "sheet_name": backup})
            expected = original if has_target else [*original, target]
            if await self.sheets() != expected:
                raise ResetError("Итоговый список листов не совпал с ожидаемым.")
        except (ResetError, TableError) as error:
            raise ResetError(
                f"{error} При проверке книги ищите временные листы «{staged}» и «{backup}»."
            ) from error


async def reset_workbook(settings: ExcelSettings, *, confirmed: bool = False) -> None:
    if confirmed is not True:
        raise ResetError("Для сброса требуется явное подтверждение.")
    if not settings.excel_file_path.is_file():
        raise ResetError("Книга заявок отсутствует. Запустите python -m scripts.init_excel.")

    client = McpClient(
        settings.mcp_python_path,
        connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
        tool_timeout_seconds=settings.mcp_tool_timeout_seconds,
    )
    async with client.operation() as operation:
        missing = _REQUIRED_TOOLS - {tool.name for tool in operation.tools}
        if missing:
            raise ResetError(f"MCP-сервер не предоставил инструменты {', '.join(sorted(missing))}.")
        await _SheetReset(operation, settings).run(settings.excel_sheet_name)


def main() -> int:
    try:
        settings = load_excel_settings()
        print(f"Будут восстановлены исходные заявки на листе «{settings.excel_sheet_name}».")
        print(f"Книга {settings.excel_file_path}")
        try:
            confirmed = input("Для подтверждения введите СБРОС. ").strip() == "СБРОС"
        except (EOFError, KeyboardInterrupt):
            confirmed = False
        if not confirmed:
            print("Сброс отменён.")
            return 0
        asyncio.run(reset_workbook(settings, confirmed=True))
    except (SettingsError, McpConnectionError, ResetError, OSError) as error:
        print(f"Сброс завершился ошибкой. {error}", file=sys.stderr)
        return 1

    print("Три исходные заявки восстановлены и проверены через MCP.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
