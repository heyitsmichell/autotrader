"""Read-only experiment results (data/experiments): leaderboard, registry, markdown reports."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..config import env

router = APIRouter()
MAX_REPORT_BYTES = 200 * 1024
_BIASED = {"sp500", "app87"}      # current constituents / hindsight-selected stock universes


def _root() -> Path:
    return (Path(env.DATA_DIR) / "experiments").resolve()


def _f(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def _row(r: dict) -> dict:
    uni = (r.get("universe") or "").strip()
    return {"name": r.get("name", ""), "kind": r.get("kind", ""), "cagr": _f(r.get("cagr")),
            "sharpe": _f(r.get("sharpe")), "maxdd": _f(r.get("maxdd")), "dsr": _f(r.get("dsr")),
            "universe": uni, "survivorship_biased": uni.lower() in _BIASED
            or uni.lower().startswith("sp500")}


def _csv(path: Path) -> list[dict]:
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


def _title(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#"):
                    return line.lstrip("#").strip()
    except OSError:
        pass
    return path.stem


def _bt_root() -> Path:
    return (Path(env.DATA_DIR) / "backtests").resolve()


def _safe(root: Path, rel: str) -> Path:
    rel = rel or ""
    if rel.replace("\\", "/").startswith("backtests/"):
        root, rel = _bt_root(), rel.replace("\\", "/")[len("backtests/"):]
    if not rel or rel.startswith(("/", "\\")) or ":" in rel or not rel.lower().endswith(".md"):
        raise HTTPException(400, "path must be a relative .md path under data/experiments")
    if ".." in Path(rel.replace("\\", "/")).parts:
        raise HTTPException(400, "invalid path")
    try:
        real = (root / rel).resolve()
        real.relative_to(root)
    except (ValueError, OSError):
        raise HTTPException(400, "invalid path") from None
    return real


@router.get("/api/experiments")
async def experiments():
    root = _root()
    reg = _csv(root / "trials_registry.csv")
    by_lab: dict[str, int] = {}
    for r in reg:
        by_lab[r.get("lab", "")] = by_lab.get(r.get("lab", ""), 0) + 1
    rows = _csv(root / "rotation" / "leaderboard.csv")
    scored = [r for r in rows if _f(r.get("dsr")) is not None]
    scored.sort(key=lambda r: _f(r["dsr"]), reverse=True)
    bench = [_row(r) for r in rows if r.get("kind") == "benchmark"]
    intraday = None
    try:
        data = json.loads((root / "intraday_breakout" / "summary.json").read_text(encoding="utf-8"))
        intraday = data.get("v2") if isinstance(data, dict) else None
    except (OSError, ValueError):
        pass
    reports = []
    if root.is_dir():
        for p in sorted(root.rglob("*.md")):
            try:
                real = p.resolve()
                real.relative_to(root)
                stt = real.stat()
            except (ValueError, OSError):
                continue
            reports.append({"path": p.relative_to(root).as_posix(), "title": _title(real),
                            "modified": datetime.fromtimestamp(stt.st_mtime, timezone.utc)
                            .isoformat(timespec="seconds"), "bytes": stt.st_size})
    bt = _bt_root()
    if bt.is_dir():
        for p in sorted(bt.rglob("*.md")):
            try:
                real = p.resolve()
                real.relative_to(bt)
                stt = real.stat()
            except (ValueError, OSError):
                continue
            reports.append({"path": "backtests/" + p.relative_to(bt).as_posix(), "title": _title(real),
                            "modified": datetime.fromtimestamp(stt.st_mtime, timezone.utc)
                            .isoformat(timespec="seconds"), "bytes": stt.st_size})
    return {"registry": {"n_trials": len(reg), "by_lab": by_lab},
            "leaderboard": [_row(r) for r in scored[:25]],
            # best survivorship-free rows: the top 25 overall can be all biased universes
            "leaderboard_clean": [x for x in (_row(r) for r in scored
                                              if r.get("kind") != "benchmark")
                                  if not x["survivorship_biased"]][:15],
            "benchmarks": bench,
            "intraday_entry": intraday, "reports": reports}


@router.get("/api/experiments/report")
async def experiment_report(path: str):
    p = _safe(_root(), path)
    if not p.is_file():
        raise HTTPException(404, "report not found")
    with p.open("rb") as f:
        text = f.read(MAX_REPORT_BYTES).decode("utf-8", errors="replace")
    return {"path": path.replace("\\", "/"), "text": text}
