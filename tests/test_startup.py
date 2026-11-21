import os
import shutil
import subprocess

import pytest

from config import settings
from scripts import check_startup


def test_startup_reports_missing_dependencies(monkeypatch, capsys):
    monkeypatch.setattr(check_startup, "find_spec", lambda name: None if name == "streamlit" else object())

    assert check_startup.main() == 1
    assert "streamlit" in capsys.readouterr().err


def test_startup_reports_configuration_error(monkeypatch, capsys):
    def configuration_error():
        raise settings.SettingsError("Укажите OPENAI_MODEL.")

    monkeypatch.setattr(settings, "load_settings", configuration_error)

    assert check_startup.main() == 1
    assert "OPENAI_MODEL" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="start.bat запускается на Windows.")
def test_batch_reports_missing_project_python(tmp_path):
    shutil.copyfile(settings.PROJECT_ROOT / "start.bat", tmp_path / "start.bat")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "start.bat"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "Project Python was not found" in result.stderr
