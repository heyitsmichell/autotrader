"""Ops: process matching / parsing used by `scripts/trader.py stop` (M17, M18)."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("trader_script", ROOT / "scripts" / "trader.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _load()
REPO = Path("C:/Users/me/Trader")
REPO_POSIX = Path("/home/me/Trader")


def test_matches_module_entry_in_repo():
    cmd = r'"C:\Users\me\Trader\.venv\Scripts\python.exe" -m app.main'
    assert T.is_app_cmdline(cmd, REPO)


def test_matches_uvicorn_entry_in_repo():
    cmd = "/home/me/Trader/.venv/bin/python -m uvicorn app.main:app --port 8000"
    assert T.is_app_cmdline(cmd, REPO_POSIX)


def test_matches_launcher_run_in_repo():
    cmd = r'"C:\Users\me\Trader\.venv\Scripts\python.exe" C:\Users\me\Trader\scripts\trader.py run'
    assert T.is_app_cmdline(cmd, REPO)
    assert T.is_app_cmdline("/home/me/Trader/.venv/bin/python /home/me/Trader/scripts/trader.py", REPO_POSIX)


def test_does_not_match_stop_or_other_subcommands():
    for sub in ("stop", "auth", "setup", "install", "uninstall", "status"):
        cmd = f"/home/me/Trader/.venv/bin/python /home/me/Trader/scripts/trader.py {sub}"
        assert not T.is_app_cmdline(cmd, REPO_POSIX), sub


def test_does_not_match_unrelated_trader_py():
    assert not T.is_app_cmdline("python /opt/other/trader.py", REPO_POSIX)
    assert not T.is_app_cmdline("python /opt/other/scripts/trader.py run", REPO_POSIX)
    assert not T.is_app_cmdline("python -m app.main", REPO_POSIX)  # other checkout/no repo path
    assert not T.is_app_cmdline("", REPO_POSIX)
    assert not T.is_app_cmdline("vim /home/me/Trader/notes.txt", REPO_POSIX)


def test_lock_pid_match_does_not_require_repo_path():
    assert T.is_app_cmdline("python -m app.main", REPO_POSIX, require_repo=False)
    assert not T.is_app_cmdline("python something_else.py", REPO_POSIX, require_repo=False)


def test_find_app_pids_never_returns_own_or_parent():
    own, parent = 100, 50
    procs = [
        (own, parent, "/home/me/Trader/.venv/bin/python -m app.main"),
        (parent, 1, "/home/me/Trader/.venv/bin/python -m app.main"),
        (200, 1, "/home/me/Trader/.venv/bin/python -m app.main"),
        (300, 1, "python /elsewhere/trader.py"),
        (400, 1, "/home/me/Trader/.venv/bin/python /home/me/Trader/scripts/trader.py stop"),
    ]
    assert T.find_app_pids(procs, REPO_POSIX, {own, parent}) == [200]


def test_find_app_pids_uses_real_own_pid_exclusion():
    procs = [(os.getpid(), os.getppid(), f"{ROOT.as_posix()}/.venv/bin/python -m app.main")]
    assert T.find_app_pids(procs, ROOT, {os.getpid(), os.getppid()}) == []


CIM = (
    '"ProcessId","ParentProcessId","CommandLine"\r\n'
    '"4","0",\r\n'
    '"1234","1000","""C:\\Users\\me\\Trader\\.venv\\Scripts\\python.exe"" -m app.main"\r\n'
    '"5678","1000","""C:\\Python\\python.exe"" C:\\x\\trader.py, with comma"\r\n'
)


def test_parse_cim_csv():
    rows = T.parse_cim_csv(CIM)
    pids = {p: (pp, c) for p, pp, c in rows}
    assert pids[1234][0] == 1000
    assert pids[1234][1].endswith("-m app.main")
    assert '"C:\\Users\\me\\Trader' in pids[1234][1]
    assert pids[5678][1].endswith("with comma")
    assert T.is_app_cmdline(pids[1234][1], REPO)
    assert not T.is_app_cmdline(pids[5678][1], REPO)
    # BOM and garbage tolerated
    assert T.parse_cim_csv("\ufeff" + CIM)
    assert T.parse_cim_csv("") == []
    assert T.parse_cim_csv("nonsense") == []


def test_parse_wmic_csv_with_commas_in_command_line():
    text = (
        "\r\r\nNode,CommandLine,ProcessId\r\r\n"
        'PC,"C:\\Users\\me\\Trader\\.venv\\Scripts\\python.exe" -m app.main,1234\r\r\n'
        "PC,python a,b,c,77\r\r\n"
        "PC,,0\r\r\n"
    )
    rows = {p: c for p, _pp, c in T.parse_wmic_csv(text)}
    assert rows[1234].endswith("-m app.main")
    assert rows[77] == "python a,b,c"
    assert T.is_app_cmdline(rows[1234], REPO)


def test_parse_ps():
    text = " 10 1 /usr/bin/python -m app.main\n  11 10 bash -c x\nbad line\n"
    assert T.parse_ps(text) == [(10, 1, "/usr/bin/python -m app.main"), (11, 10, "bash -c x")]


def test_pid_in_tasklist_csv():
    out = '"python.exe","1234","Console","1","50,000 K"\r\n'
    assert T.pid_in_tasklist_csv(out, 1234)
    assert not T.pid_in_tasklist_csv(out, 234)
    assert not T.pid_in_tasklist_csv("INFO: No tasks are running which match the specified criteria.", 1)


def test_read_lock_file_and_token(tmp_path):
    lock = tmp_path / "trader.lock"
    lock.write_text("4321:8000")
    assert T.read_lock_file(lock) == (4321, 8000)
    lock.write_text("garbage")
    assert T.read_lock_file(lock) is None
    assert T.read_lock_file(tmp_path / "missing") is None
    tok = tmp_path / "api_token"
    assert T.read_api_token(tok) is None
    tok.write_text("abc\n")
    assert T.read_api_token(tok) == "abc"


def test_stop_never_signals_own_pid(monkeypatch, tmp_path):
    """Lock file pointing at our own pid must not be killed."""
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path))
    (tmp_path / "trader.lock").write_text(f"{os.getpid()}:8000")
    launcher = T.TraderLauncher()

    class S:
        port = 8000
    launcher._settings = S()
    sent = []
    monkeypatch.setattr(launcher, "_signal_pid", lambda pid, force: sent.append(pid))
    monkeypatch.setattr(T, "list_processes", lambda: [
        (os.getpid(), os.getppid(), f"{ROOT.as_posix()}/.venv/bin/python {ROOT.as_posix()}/scripts/trader.py stop"),
    ])
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
    assert launcher.stop() is True
    assert sent == []
