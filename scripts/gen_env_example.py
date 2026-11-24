#!/usr/bin/env python3
"""Generate .env.example from the Settings definition.

The loader (app/config/loader.py) records every environment variable it reads together
with its code default (app.config.REGISTRY). This script keeps the hand-written sections
and comments of the existing .env.example, and:

  * applies OVERRIDES (values that .env.example deliberately sets differently from the
    code default, or that a contract changed),
  * drops duplicate KEY= lines (the first one wins; dotenv would use the last),
  * blanks secrets,
  * appends every variable the loader reads that the file does not mention yet, in
    grouped sections, each with the default and a short comment taken from the
    Settings field comments.

Usage:
    python scripts/gen_env_example.py            # rewrite .env.example
    python scripts/gen_env_example.py --check    # exit 1 when it is out of sync
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET = ROOT / ".env.example"

# Values .env.example sets on purpose (they win over the file and over the code default).
OVERRIDES: dict[str, str] = {
    "SIGNAL_REFRESH_MINUTES": "5",                 # was 1 (L9): news/TA refresh every minute is too hot
    "ELIGIBILITY_EARNINGS_BLACKOUT_DAYS": "3",     # contract section 2: default 2 -> 3
    "BACKTEST_ML_ENABLED": "true",                 # contract section 2: default False -> True
}

SECRET_HINTS = ("KEY", "TOKEN", "PASSWORD", "SECRET", "WEBHOOK", "TOPIC")
SECRET_ALLOW = {"API_TOKEN_FILE", "WEBULL_TOKEN_DIR", "TOKEN_WARN_DAYS", "STREAM_HEARTBEAT_SECONDS"}

# (prefix, section title) in display order for variables appended by the generator.
SECTIONS = [
    ("OPTIONS_", "Options"),
    ("RISK_", "Risk"), ("MAX_", "Risk"), ("ENTRY_", "Entry quality"), ("BLOCK_", "Entry quality"),
    ("EQUITY_", "Quotes"), ("ELIGIBILITY_", "Eligibility"), ("DB_", "Storage"),
    ("BROKER_", "Broker"), ("WEBULL_", "Broker"), ("FEED_", "Market feed"),
    ("LLM_", "LLM"), ("YFINANCE_", "yfinance"), ("HEADLINE_", "News"), ("NEWS_", "News"),
    ("API_", "API"), ("STREAM_", "API"), ("ML_", "ML"), ("BACKTEST_", "Backtest"),
    ("ALERT_", "Alerts"),
]


def _is_secret(name: str) -> bool:
    return name not in SECRET_ALLOW and any(h in name for h in SECRET_HINTS) \
        and not name.endswith(("_DIR", "_FILE", "_DAYS", "_SECONDS", "_MINUTES"))


def _fmt(name: str, default: object) -> str:
    if _is_secret(name):
        return ""
    if name in OVERRIDES:
        return OVERRIDES[name]
    if isinstance(default, Path) or (isinstance(default, str) and Path(default).is_absolute()):
        return ""                             # path defaults depend on DATA_DIR; leave empty
    if isinstance(default, bool):
        return "true" if default else "false"
    if isinstance(default, (list, tuple, set, frozenset)):
        return ",".join(str(x) for x in default)
    return "" if default is None else str(default)


def _comments() -> dict[str, str]:
    """ENV_NAME -> short comment, from the trailing comments of Settings field lines."""
    out: dict[str, str] = {}
    src = (ROOT / "app" / "config" / "settings.py").read_text(encoding="utf-8")
    pat = re.compile(r"^\s+\w+\s*:[^#\n]*#\s*([A-Z][A-Z0-9_]+)\b\s*(?:\([^)]*\))?\s*[:)]?\s*(.*)$")
    for line in src.splitlines():
        m = pat.match(line)
        if m:
            out.setdefault(m.group(1), m.group(2).strip().rstrip("."))
    return out


def _section(name: str) -> str:
    for prefix, title in SECTIONS:
        if name.startswith(prefix):
            return title
    return "Other"


def generate(existing: str, registry: dict[str, object], comments: dict[str, str]) -> str:
    mentioned: set[str] = set()
    key_re = re.compile(r"^(#\s*)?([A-Z][A-Z0-9_]*)=(.*)$")
    active_re = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
    lines = existing.splitlines()
    last_value: dict[str, str] = {}      # dotenv: the last duplicate wins, so keep that value
    for line in lines:
        m = active_re.match(line)
        if m:
            last_value[m.group(1)] = m.group(2)
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = key_re.match(line)
        if m:
            mentioned.add(m.group(2))
        m = active_re.match(line)
        if m:
            name = m.group(1)
            if name in seen:
                continue                      # duplicate: keep the first position, last value
            seen.add(name)
            rest = last_value[name]
            if name in OVERRIDES:
                cm = re.search(r"\s+#.*$", rest)
                line = f"{name}={OVERRIDES[name]}{cm.group(0) if cm else ''}"
            elif _is_secret(name) and rest.strip() and not rest.strip().startswith("#"):
                line = f"{name}="
            else:
                line = f"{name}={rest}"
        out.append(line)
    text = "\n".join(out).rstrip("\n") + "\n"

    missing = [n for n in registry if n not in mentioned]
    if missing:
        groups: dict[str, list[str]] = {}
        for n in missing:
            groups.setdefault(_section(n), []).append(n)
        text += "\n# --- Settings added by the QA fix programme (generated by scripts/gen_env_example.py) ---\n"
        for title in [t for _, t in SECTIONS] + ["Other"]:
            names = groups.pop(title, None)
            if not names:
                continue
            text += f"\n# {title}\n"
            for n in names:
                c = comments.get(n)
                if c:
                    text += f"# {c}\n"
                text += f"{n}={_fmt(n, registry[n])}\n"
    return text


def build() -> str:
    from app.config import REGISTRY, load_settings
    load_settings()                           # populates REGISTRY
    existing = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    return generate(existing, dict(REGISTRY), _comments())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if .env.example is out of sync")
    args = ap.parse_args(argv)
    new = build()
    old = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    if args.check:
        if new == old:
            return 0
        diff = difflib.unified_diff(old.splitlines(), new.splitlines(), ".env.example (current)",
                                    ".env.example (generated)", lineterm="", n=0)
        print("\n".join(list(diff)[:40]))
        print("\n.env.example is out of sync; run: python scripts/gen_env_example.py", file=sys.stderr)
        return 1
    if new != old:
        TARGET.write_text(new, encoding="utf-8", newline="\n")
        print(f"updated {TARGET}")
    else:
        print("already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
