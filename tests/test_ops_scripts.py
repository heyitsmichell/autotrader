"""Ops: shell/bat script checks (M19, L7, L8, L15)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")


def _bash_detect(tmp_path, create):
    (tmp_path / ".venv").mkdir()
    for rel in create:
        f = tmp_path / ".venv" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    shutil.copy(ROOT / "_bootstrap.sh", tmp_path / "_bootstrap.sh")
    r = subprocess.run(
        [BASH, "-c", 'cd "$1" && source ./_bootstrap.sh && detect_venv_py; echo "[$VENV_PY]"', "x", str(tmp_path)],
        capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_detects_posix_venv(tmp_path):
    assert _bash_detect(tmp_path, ["bin/python"]) == "[./.venv/bin/python]"


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_detects_windows_venv(tmp_path):
    assert _bash_detect(tmp_path, ["Scripts/python.exe"]) == "[./.venv/Scripts/python.exe]"


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_no_venv(tmp_path):
    assert _bash_detect(tmp_path, []) == "[]"


def test_shell_scripts_no_hardcoded_venv_bin():
    for name in ("setup.sh", "start.sh", "stop.sh", "auth.sh", "install_autostart.sh"):
        text = (ROOT / name).read_text()
        assert "./.venv/bin/python" not in text, name
        assert "_bootstrap.sh" in text, name


def test_windows_bats_exist_and_call_subcommands():
    stop = (ROOT / "Stop_Trader.bat").read_text()
    auth = (ROOT / "Auth_Trader.bat").read_text()
    assert r".venv\Scripts\python.exe scripts\trader.py stop" in stop
    assert r".venv\Scripts\python.exe scripts\trader.py auth" in auth


def test_start_bat_polls_status_not_fixed_timeout():
    text = (ROOT / "Start_Trader.bat").read_text()
    assert "timeout /t" not in text
    assert "/api/status" in text


def test_start_command_polls_status():
    text = (ROOT / "Start_Trader.command").read_text()
    assert "sleep 4;" not in text
    assert "/api/status" in text


def test_backtest_matrix_has_no_hardcoded_user_path():
    text = (ROOT / "run_backtest_matrix.py").read_text()
    assert "/Users/" not in text
    assert "Path(__file__)" in text


def test_requirements_pinned():
    req = (ROOT / "requirements.txt").read_text()
    for pkg in ("requests", "tzdata"):
        assert re.search(rf"^{pkg}==\S+", req, re.M), pkg
    dev = (ROOT / "requirements-dev.txt").read_text()
    for pkg in ("pytest", "pytest-asyncio"):
        assert re.search(rf"^{pkg}==\S+", dev, re.M), pkg
    assert "pytest-cov" in dev


def test_pc_launchers_have_bot_disabled_guard():
    for name in ("Start_Trader.bat", "Start_Trader.command", "start.sh", "install_autostart.sh"):
        assert "PC_BOT_DISABLED" in (ROOT / name).read_text(encoding="utf-8"), name
    assert "PC_BOT_DISABLED" in (ROOT / "deploy/oracle/push_to_server.ps1").read_text(encoding="utf-8")
