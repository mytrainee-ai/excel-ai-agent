"""Создаёт учебную книгу через Excel MCP Server."""

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from config.settings import ExcelSettings, SettingsError, load_excel_settings
from mcp_client.client import McpClient, McpConnectionError
from services.validation import HEADERS


INITIAL_REQUESTS = (
    (101, "Иван Петров", "Не работает принтер", "Новый"),
    (102, "Елена Кузнецова", "Не удаётся подключиться к Wi-Fi", "Закрыт"),
    (103, "Мария Соколова", "Не открывается корпоративная почта", "В работе"),
)


class InitializationError(RuntimeError):
    """Учебную книгу не удалось создать или проверить."""


async def _prepare_workbook(client: McpClient, path: Path, sheet_name: str) -> None:
    rows = [list(row) for row in (HEADERS, *INITIAL_REQUESTS)]
    sheet = {"workbook_path": str(path), "sheet_name": sheet_name}

    async with client.operation() as operation:
        calls = (
            ("create_workbook", {"file_path": str(path)}),
            ("create_sheet", sheet),
            ("write_range", sheet | {"start_cell": "A1", "data": rows}),
            ("read_range", sheet | {"range_ref": f"A1:D{len(rows)}"}),
        )
        available = {tool.name for tool in operation.tools}
        missing = [name for name, _ in calls if name not in available]
        if missing:
            raise InitializationError(f"Excel MCP Server не предоставил инструменты {', '.join(missing)}.")

        for name, arguments in calls[:2]:
            result = await operation.call_tool(name, arguments)
            if not result.ok:
                raise InitializationError(f"MCP-инструмент {name} вернул ошибку ({result.status}).")

        write = await operation.call_tool(*calls[2])
        if write.status == "tool_error":
            raise InitializationError("MCP-инструмент write_range сообщил об ошибке записи.")

        # При тайм-ауте и других неопределённых результатах выясняем состояние чтением.
        read = await operation.call_tool(*calls[3])
        if not read.ok:
            raise InitializationError(f"Не удалось перечитать записанный диапазон ({read.status}).")
        actual = read.data.get("data") if isinstance(read.data, dict) else None
        if actual != rows or any(type(row[0]) is not int for row in actual[1:]):
            raise InitializationError("Данные после чтения не совпали с учебным набором.")


async def initialize_workbook(settings: ExcelSettings) -> bool:
    """Публикует проверенную книгу и возвращает False, если файл уже существует."""
    destination = settings.excel_file_path
    if destination.exists():
        return False
    if settings.mcp_max_tool_calls < 4:
        raise InitializationError("Для инициализации MCP_MAX_TOOL_CALLS должен быть не меньше 4.")

    client = McpClient(
        settings.mcp_python_path,
        connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
        tool_timeout_seconds=settings.mcp_tool_timeout_seconds,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".init-", dir=destination.parent) as directory:
        staged = Path(directory) / destination.name
        await _prepare_workbook(client, staged, settings.excel_sheet_name)
        try:
            # Создаём запись каталога для готовой книги без замены существующего файла.
            destination.hardlink_to(staged)
        except FileExistsError:
            return False
    return True


def main() -> int:
    try:
        settings = load_excel_settings()
        created = asyncio.run(initialize_workbook(settings))
    except (SettingsError, InitializationError, McpConnectionError, OSError) as error:
        print(f"Инициализация Excel завершилась ошибкой. {error}", file=sys.stderr)
        return 1

    path = settings.excel_file_path
    if created:
        print(f"Книга создана и проверена через MCP. {path}")
    else:
        print(f"Книга уже существует. Файл оставлен без изменений. {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
