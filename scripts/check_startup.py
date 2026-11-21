"""Проверки перед запуском Streamlit из start.bat."""

import sys
from importlib.util import find_spec


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        print("Создайте окружение проекта на Python 3.12.", file=sys.stderr)
        return 1

    missing = [name for name in ("streamlit", "openai", "mcp", "dotenv") if find_spec(name) is None]
    if missing:
        print(
            f"Отсутствуют модули {', '.join(missing)}. Установите зависимости из requirements.txt.",
            file=sys.stderr,
        )
        return 1

    from config.settings import SettingsError, load_settings

    try:
        settings = load_settings()
        if not settings.excel_file_path.is_file():
            raise SettingsError("Книга заявок отсутствует. Запустите python -m scripts.init_excel.")
    except (SettingsError, OSError) as error:
        print(f"Не удалось запустить Excel AI Agent. {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
