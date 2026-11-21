import os
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from config.settings import PROJECT_ROOT, SettingsError, load_settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in os.environ:
        if name.startswith(("OPENAI_", "EXCEL_", "MCP_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "OPENAI_API_KEY=test-key\n"
        "OPENAI_MODEL=test-model\n"
        f"MCP_PYTHON_PATH={sys.executable}\n"
        "EXCEL_SHEET_NAME=Заявки\n",
        encoding="utf-8",
    )
    return path


def test_loads_settings_and_anchors_workbook_to_project(env_file, monkeypatch):
    monkeypatch.chdir(env_file.parent)
    settings = load_settings(env_file)

    assert settings.excel_file_path == PROJECT_ROOT / "data" / "requests.xlsx"
    assert settings.excel_sheet_name == "Заявки"
    assert settings.mcp_python_path == Path(sys.executable).resolve()
    assert settings.openai_model == "test-model"
    assert "test-key" not in repr(settings)
    assert settings.mcp_max_tool_calls == 10
    assert settings.mcp_tool_timeout_seconds == 15
    assert settings.mcp_connect_timeout_seconds == 30


def test_process_environment_overrides_dotenv(env_file, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "environment-model")
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_SECONDS", "20")

    settings = load_settings(env_file)

    assert settings.openai_model == "environment-model"
    assert settings.mcp_tool_timeout_seconds == 20


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPENAI_API_KEY", ""),
        ("OPENAI_MODEL", ""),
        ("OPENAI_BASE_URL", "not-a-url"),
        ("MCP_PYTHON_PATH", "missing/python.exe"),
        ("MCP_MAX_TOOL_CALLS", "0"),
        ("MCP_TOOL_TIMEOUT_SECONDS", "1.5"),
        ("MCP_CONNECT_TIMEOUT_SECONDS", "-1"),
        ("EXCEL_FILE_PATH", "requests.csv"),
        ("EXCEL_SHEET_NAME", " "),
    ],
)
def test_invalid_settings_report_field_name(env_file, monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(SettingsError, match=name) as error:
        load_settings(env_file)

    assert "test-key" not in str(error.value)


def test_start_page_reports_missing_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")

    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()

    assert not app.exception
    assert "OPENAI_API_KEY" in app.error[0].value


def test_start_page_reports_missing_workbook(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("MCP_PYTHON_PATH", sys.executable)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("EXCEL_FILE_PATH", str(tmp_path / "requests.xlsx"))
    monkeypatch.setenv("EXCEL_SHEET_NAME", "Заявки")
    monkeypatch.setenv("MCP_MAX_TOOL_CALLS", "10")
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("MCP_CONNECT_TIMEOUT_SECONDS", "30")

    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()

    assert not app.exception
    assert "Книга заявок отсутствует" in app.error[0].value
    assert "test-key" not in app.error[0].value
