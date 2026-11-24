#!/usr/bin/env python3
"""
Build a share-safe zip of the Trader project, excluding secrets and build artifacts.

Excludes: .env and .env.* (except .env.example), *.db/-wal/-shm, *.pem, *.key, *.log,
data/api_token, .venv/, data/, logs/, conf/, __pycache__/, web/node_modules/,
web/.next/, web/out/, *.pyc, webull_data_streaming_sdk.log, .scratch/, .git/
"""

import os
import re
import sys
import zipfile
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_PATTERNS = [
    r"(^|/)\.env(\..*)?$",         # .env, .env.local, .env.prod ... (anywhere)
    r"\.db$", r"\.db-wal$", r"\.db-shm$",   # SQLite databases and sidecars
    r"\.pem$", r"\.key$",          # private keys / certs
    r"\.log$",                      # logs anywhere
    r"(^|/)api_token$",            # API token file (data/api_token)
    r"^\.venv(/|$)",               # .venv directory
    r"^data(/|$)",                 # data directory
    r"^logs(/|$)",                 # logs directory
    r"^conf(/|$)",                 # conf directory
    r"(^|/)__pycache__(/|$)",      # __pycache__ directories
    r"\.pyc$",                     # .pyc files
    r"^web/node_modules(/|$)",     # web/node_modules directory
    r"^web/\.next(/|$)",           # web/.next directory
    r"^web/out(/|$)",              # web/out directory
    r"^webull_data_streaming_sdk\.log$",  # SDK log at root
    r"^\.git(/|$)",                # .git directory (not .gitignore)
    r"^\.pytest_cache(/|$)",       # .pytest_cache directory
    r"^\.scratch(/|$)",            # .scratch directory
    r"\.zip$",                     # zip files
]

COMPILED_EXCLUDES = [re.compile(p) for p in EXCLUDE_PATTERNS]


# Anything matching one of these must never be in the archive (post-check).
# .env.example is the one allowed .env* file.
SECRET_PATTERNS = [re.compile(p) for p in (
    r"(^|/)\.env(\..*)?$",
    r"\.db$", r"\.db-wal$", r"\.db-shm$",
    r"\.pem$", r"\.key$", r"\.log$",
    r"(^|/)api_token$",
)]


def is_allowed_exception(rel_path: str) -> bool:
    return rel_path.replace("\\", "/").split("/")[-1] == ".env.example"


def should_exclude(rel_path: str) -> bool:
    """Check if a relative path matches any exclude pattern."""
    rel_path = rel_path.replace("\\", "/")
    if is_allowed_exception(rel_path):
        return False
    for pattern in COMPILED_EXCLUDES:
        if pattern.search(rel_path):
            return True
    return False


def find_secret_entries(names) -> list:
    """Names (archive paths) that look like secrets and must not be shipped."""
    bad = []
    for n in names:
        n = n.replace("\\", "/")
        if is_allowed_exception(n):
            continue
        if any(p.search(n) for p in SECRET_PATTERNS):
            bad.append(n)
    return bad


def get_version() -> str:
    """Extract version from git tag (preferred), pyproject.toml, or setup.py. Falls back to 'dev'."""
    # Try git tag first (preferred)
    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(ROOT), "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    # Try pyproject.toml
    pyproject = ROOT / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text()
        m = re.search(r'version\s*=\s*"([^"]+)"', content)
        if m:
            return m.group(1)

    # Try setup.py
    setup = ROOT / "setup.py"
    if setup.exists():
        content = setup.read_text()
        m = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
        if m:
            return m.group(1)

    return "dev"


def main():
    version = get_version()
    date_str = datetime.now().strftime("%Y%m%d")
    zip_name = f"trader-{version}-{date_str}.zip"
    output_path = ROOT / zip_name

    print(f"Building share-safe package: {zip_name}")
    print(f"Source: {ROOT}")
    print(f"Output: {output_path}")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        copied = 0
        skipped = 0

        print("Copying files to temp directory...")
        for file_path in ROOT.rglob("*"):
            if not file_path.is_file():
                continue

            rel = file_path.relative_to(ROOT)
            rel_str = str(rel).replace("\\", "/")

            if should_exclude(rel_str):
                skipped += 1
                continue

            dest = temp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dest)
            copied += 1

        print(f"Copied {copied} files, skipped {skipped} excluded files")

        print("Creating zip archive...")
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in temp_path.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(temp_path)
                    zf.write(file_path, arcname)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"Done! Package created: {output_path} ({size_mb:.1f} MB)")

    # Post-check: no secret-looking file may be in the archive. Fail the build and
    # leave no zip behind.
    with zipfile.ZipFile(output_path, "r") as zf:
        names = zf.namelist()
    bad = find_secret_entries(names)
    if bad:
        print("ERROR: secret-looking files found in archive, build failed:", file=sys.stderr)
        for n in bad:
            print(f"  {n}", file=sys.stderr)
        output_path.unlink(missing_ok=True)
        sys.exit(1)
    print("Verified: no .env*, *.db, *.pem, *.key, *.log or api_token in the archive")

    # List archive contents for verification
    print("\nArchive contents (first 30):")
    for n in sorted(names)[:30]:
        print(f"  {n}")
    if len(names) > 30:
        print(f"  ... and {len(names) - 30} more files")


if __name__ == "__main__":
    main()