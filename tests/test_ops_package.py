"""Ops: scripts/package.py exclusion and post-check (M20)."""
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("package_script", ROOT / "scripts" / "package.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _load()


@pytest.mark.parametrize("rel", [
    ".env", ".env.local", ".env.prod", "sub/.env.staging", "x.db", "data/trader.db",
    "trader.db-wal", "trader.db-shm", "a/cert.pem", "id.key", "run.log", "web/x.log",
    "data/api_token", ".venv/bin/python", "conf/token", "app/__pycache__/a.pyc",
    "web/node_modules/x/y.js", "old.zip",
])
def test_excluded(rel):
    assert P.should_exclude(rel), rel


@pytest.mark.parametrize("rel", [
    ".env.example", "app/main.py", "README.md", "web/src/app/page.tsx",
    "requirements.txt", ".gitignore", "scripts/package.py",
])
def test_included(rel):
    assert not P.should_exclude(rel), rel


def test_find_secret_entries():
    names = ["app/main.py", ".env.example", ".env", "x/.env.local", "a.db", "k.pem", "k.key",
             "run.log", "data/api_token", "a.db-wal"]
    assert sorted(P.find_secret_entries(names)) == sorted(
        [".env", "x/.env.local", "a.db", "k.pem", "k.key", "run.log", "data/api_token", "a.db-wal"])
    assert P.find_secret_entries(["app/main.py", ".env.example"]) == []


def _make_tree(root: Path):
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text("x = 1\n")
    (root / ".env.example").write_text("A=\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / ".env.local").write_text("SECRET=2\n")
    (root / "t.db").write_text("db")
    (root / "k.pem").write_text("pem")
    (root / "sub").mkdir()
    (root / "sub" / "run.log").write_text("log")
    (root / "data").mkdir()
    (root / "data" / "api_token").write_text("tok")


def test_main_builds_clean_zip(tmp_path, monkeypatch):
    _make_tree(tmp_path)
    monkeypatch.setattr(P, "ROOT", tmp_path)
    monkeypatch.setattr(P, "get_version", lambda: "t")
    P.main()
    zips = list(tmp_path.glob("trader-t-*.zip"))
    assert len(zips) == 1
    names = zipfile.ZipFile(zips[0]).namelist()
    norm = sorted(n.replace("\\", "/") for n in names)
    assert norm == [".env.example", "app/main.py"]


def test_main_fails_and_leaves_no_zip_when_secret_slips_in(tmp_path, monkeypatch):
    _make_tree(tmp_path)
    monkeypatch.setattr(P, "ROOT", tmp_path)
    monkeypatch.setattr(P, "get_version", lambda: "t")
    monkeypatch.setattr(P, "should_exclude", lambda rel: False)  # simulate a broken exclude list
    with pytest.raises(SystemExit) as ei:
        P.main()
    assert ei.value.code != 0
    assert list(tmp_path.glob("*.zip")) == []
