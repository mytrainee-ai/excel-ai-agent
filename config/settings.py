"""Загрузка и проверка конфигурации приложения."""

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SettingsError(ValueError):
    """Ошибка конфигурации, которую можно показать пользователю."""


@dataclass(frozen=True)
class ExcelSettings:
    excel_file_path: Path
    excel_sheet_name: str
    mcp_python_path: Path
    mcp_max_tool_calls: int
    mcp_tool_timeout_seconds: int
    mcp_connect_timeout_seconds: int


@dataclass(frozen=True)
class Settings(ExcelSettings):
    openai_api_key: str = field(repr=False)
    openai_base_url: str
    openai_model: str


def _load_values(env_file: Path | None) -> dict[str, str | None]:
    if env_file is None:
        env_file = PROJECT_ROOT / ".env"
    try:
        return dotenv_values(env_file, interpolate=False, encoding="utf-8-sig") | os.environ
    except (OSError, UnicodeError):
        raise SettingsError("Не удалось прочитать .env. Проверьте доступ и кодировку файла.") from None


def _setting(values: dict[str, str | None], name: str, default: str = "") -> str:
    return (values.get(name, default) or "").strip()


def _required(values: dict[str, str | None], name: str) -> str:
    value = _setting(values, name)
    if not value:
        raise SettingsError(f"Укажите {name} в .env или в окружении процесса.")
    return value


def _excel_settings(values: dict[str, str | None]) -> ExcelSettings:
    def positive_integer(name: str, default: int) -> int:
        message = f"{name} должен быть положительным целым числом."
        try:
            value = int(_setting(values, name, str(default)))
        except ValueError:
            raise SettingsError(message) from None
        if value <= 0:
            raise SettingsError(message)
        return value

    python_path = PROJECT_ROOT / _required(values, "MCP_PYTHON_PATH")
    excel_path = PROJECT_ROOT / _setting(values, "EXCEL_FILE_PATH", "data/requests.xlsx")
    if excel_path.suffix.lower() != ".xlsx":
        raise SettingsError("EXCEL_FILE_PATH должен указывать на файл .xlsx.")
    sheet_name = _setting(values, "EXCEL_SHEET_NAME", "Заявки")
    if not sheet_name:
        raise SettingsError("Укажите имя листа в EXCEL_SHEET_NAME.")

    return ExcelSettings(
        excel_file_path=excel_path.resolve(),
        excel_sheet_name=sheet_name,
        mcp_python_path=python_path.resolve(),
        mcp_max_tool_calls=positive_integer("MCP_MAX_TOOL_CALLS", 10),
        mcp_tool_timeout_seconds=positive_integer("MCP_TOOL_TIMEOUT_SECONDS", 15),
        mcp_connect_timeout_seconds=positive_integer("MCP_CONNECT_TIMEOUT_SECONDS", 30),
    )


def load_excel_settings(env_file: Path | None = None) -> ExcelSettings:
    """Загружает настройки книги и MCP для команд обслуживания."""
    return _excel_settings(_load_values(env_file))


def load_settings(env_file: Path | None = None) -> Settings:
    """Загружает полную конфигурацию приложения."""
    values = _load_values(env_file)
    api_key = _required(values, "OPENAI_API_KEY")
    model = _required(values, "OPENAI_MODEL")
    base_url = _setting(values, "OPENAI_BASE_URL", "https://api.openai.com/v1")
    try:
        url = urlsplit(base_url)
        valid_url = url.scheme in {"http", "https"} and bool(url.hostname)
    except ValueError:
        valid_url = False
    if not valid_url:
        raise SettingsError("Укажите корректный HTTP(S)-адрес в OPENAI_BASE_URL.")

    excel = _excel_settings(values)
    if not excel.mcp_python_path.is_file():
        raise SettingsError("Интерпретатор MCP не найден. Проверьте MCP_PYTHON_PATH.")
    return Settings(
        **asdict(excel),
        openai_api_key=api_key,
        openai_base_url=base_url,
        openai_model=model,
    )
