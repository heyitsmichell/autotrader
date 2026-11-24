#!/usr/bin/env python3
"""
Cross-platform launcher for AI Trader.

Usage:
    python scripts/trader.py              # Run normally
    python scripts/trader.py --install    # Install autostart (Windows/macOS/Linux)
    python scripts/trader.py --uninstall  # Remove autostart
    python scripts/trader.py --status     # Check autostart status
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import subprocess
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# NOTE: project packages (app.*) pull in third-party deps (dotenv, pydantic, ...),
# so they are imported lazily, only by the commands that need them. That lets
# `setup`, `stop`, `status` etc. run under a bare system Python before .venv exists.


def _data_dir() -> Path:
    """DATA_DIR without importing app.config (honours TRADER_DATA_DIR like config.py)."""
    return Path(os.environ.get("TRADER_DATA_DIR") or PROJECT_ROOT / "data").resolve()


# ---------------------------------------------------------------------------
# Process discovery helpers (pure parsing + matching, unit-tested in tests/test_ops_*.py)
# ---------------------------------------------------------------------------

_NON_RUN_SUBCOMMANDS = {"stop", "auth", "setup", "install", "uninstall", "status"}


def _norm(s: str) -> str:
    return s.replace("\\", "/").lower()


def read_lock_file(path: Path):
    """Parse data/trader.lock ("pid:port"). Returns (pid, port) or None."""
    try:
        parts = Path(path).read_text().strip().split(":")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None


def read_api_token(path: Path):
    try:
        tok = Path(path).read_text().strip()
        return tok or None
    except Exception:
        return None


def is_app_cmdline(cmdline: str, repo_root, require_repo: bool = True) -> bool:
    """True if a command line is the trader app: `-m app.main`, `app.main:app`
    (uvicorn), or `<repo>/scripts/trader.py` run without a non-run subcommand.

    A bare "trader.py" elsewhere never matches: the script must live in this repo.
    With require_repo=False (used for the lock-file PID, which is already trusted
    by location) the repo path need not appear in the command line.
    """
    if not cmdline:
        return False
    c = _norm(cmdline)
    root = _norm(str(repo_root)).rstrip("/")
    in_repo = bool(root) and root in c
    if "-m app.main" in c or "app.main:app" in c:
        return in_repo or not require_repo
    if in_repo and root + "/scripts/trader.py" in c:
        rest = c.split(root + "/scripts/trader.py", 1)[1].strip().strip('"').split()
        return not (rest and rest[0] in _NON_RUN_SUBCOMMANDS)
    if not require_repo and "scripts/trader.py" in c:
        rest = c.split("scripts/trader.py", 1)[1].strip().strip('"').split()
        return not (rest and rest[0] in _NON_RUN_SUBCOMMANDS)
    return False


def find_app_pids(procs, repo_root, exclude_pids) -> list:
    """procs: iterable of (pid, ppid, cmdline). Returns PIDs of app processes,
    excluding anything in exclude_pids (own pid and parent)."""
    excl = set(exclude_pids)
    return [
        pid for pid, _ppid, cmd in procs
        if pid not in excl and is_app_cmdline(cmd, repo_root, require_repo=True)
    ]


def parse_cim_csv(text: str) -> list:
    """Parse `Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,
    CommandLine | ConvertTo-Csv -NoTypeInformation` output into [(pid, ppid, cmdline)]."""
    import csv
    import io
    out = []
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return out
    header = [h.strip().lower() for h in rows[0]]
    try:
        ip, ic = header.index("processid"), header.index("commandline")
    except ValueError:
        return out
    ipp = header.index("parentprocessid") if "parentprocessid" in header else None
    for r in rows[1:]:
        if len(r) <= max(ip, ic):
            continue
        try:
            pid = int(r[ip])
            ppid = int(r[ipp]) if ipp is not None and r[ipp].strip() else 0
        except ValueError:
            continue
        out.append((pid, ppid, r[ic]))
    return out


def parse_wmic_csv(text: str) -> list:
    """Parse `wmic process get ProcessId,CommandLine /format:csv`
    (columns Node,CommandLine,ProcessId; command lines may contain commas)."""
    out = []
    for line in text.replace("\x00", "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("node,"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[-1].strip())
        except ValueError:
            continue
        out.append((pid, 0, ",".join(parts[1:-1])))
    return out


def parse_ps(text: str) -> list:
    """Parse `ps -axo pid=,ppid=,command=` output."""
    out = []
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            out.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return out


def pid_in_tasklist_csv(text: str, pid: int) -> bool:
    import csv
    import io
    for r in csv.reader(io.StringIO(text)):
        if len(r) >= 2 and r[1].strip() == str(pid):
            return True
    return False


def list_processes() -> list:
    """[(pid, ppid, cmdline)] for all processes; [] when no tool is available."""
    try:
        if sys.platform == "win32":
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine"
                     " | ConvertTo-Csv -NoTypeInformation"],
                    capture_output=True, text=True, timeout=30)
                procs = parse_cim_csv(r.stdout)
                if procs:
                    return procs
            except (OSError, subprocess.SubprocessError):
                pass
            r = subprocess.run(["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"],
                               capture_output=True, text=True, timeout=30)
            return parse_wmic_csv(r.stdout)
        r = subprocess.run(["ps", "-axo", "pid=,ppid=,command="],
                           capture_output=True, text=True, timeout=15)
        return parse_ps(r.stdout)
    except Exception as e:
        print(f"Could not list processes: {e}", file=sys.stderr)
        return []


class TraderLauncher:
    """Cross-platform launcher for AI Trader."""

    def __init__(self):
        self._settings = None
        self.lock = None
        self.sleep_preventer = None
        self.sleep_prevent_enabled = True
        self._shutdown = False

    @property
    def settings(self):
        if self._settings is None:
            from app.config import load_settings
            self._settings = load_settings()
        return self._settings

    def run(self) -> int:
        """Main entry point - acquire lock, prevent sleep, start server."""
        from app.platform import (
            acquire_single_instance_lock,
            send_desktop_notification,
            SleepPreventer,
        )
        self._notify = send_desktop_notification
        if self.sleep_prevent_enabled:
            self.sleep_preventer = SleepPreventer()

        # Acquire single-instance lock
        lock_file = _data_dir() / "trader.lock"
        try:
            self.lock = acquire_single_instance_lock(lock_file, self.settings.port)
        except SystemExit as e:
            return e.code

        # Enable sleep prevention
        if self.sleep_preventer is not None:
            if self.sleep_preventer.enable():
                print("Sleep prevention enabled")
            else:
                print("Warning: Could not enable sleep prevention", file=sys.stderr)

        # Send startup notification
        self._notify(
            "AI Trader Started",
            f"Trading engine running on port {self.settings.port}",
            urgent=False
        )

        # Set up signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        # SIGHUP is Unix-only
        if hasattr(signal, "SIGHUP"):
            try:
                signal.signal(signal.SIGHUP, self._signal_handler)
            except (OSError, ValueError):
                pass  # Not available on this platform

        # Start the FastAPI server
        try:
            return self._run_server()
        finally:
            self._cleanup()

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        print(f"\nReceived signal {signum}, shutting down...")
        self._shutdown = True

    def _run_server(self) -> int:
        """Run the FastAPI server using uvicorn."""
        import uvicorn

        # Configure uvicorn
        config = uvicorn.Config(
            "app.main:app",
            host=self.settings.host,
            port=self.settings.port,
            log_level=self.settings.log_level.lower() if hasattr(self.settings, 'log_level') else "info",
            access_log=getattr(self.settings, 'log_level', 'INFO') == "DEBUG",
            loop="asyncio",
        )

        server = uvicorn.Server(config)

        # Run server in asyncio loop
        try:
            asyncio.run(server.serve())
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(f"Server error: {e}", file=sys.stderr)
            return 1

        return 0

    def _cleanup(self):
        """Cleanup on exit."""
        if self.sleep_preventer is not None and self.sleep_preventer.is_active:
            self.sleep_preventer.disable()
            print("Sleep prevention disabled")

        if self.lock:
            self.lock.release()

        notify = getattr(self, "_notify", None)
        if notify:
            notify(
                "AI Trader Stopped",
                "Trading engine has shut down",
                urgent=False,
            )

    def setup(self) -> bool:
        """Install Python and Node dependencies, build dashboard."""
        import subprocess
        from pathlib import Path

        print("Setting up AI Trader...")

        # Create venv if not exists
        venv_path = PROJECT_ROOT / ".venv"
        if not venv_path.exists():
            print("Creating virtual environment...")
            try:
                subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)
            except subprocess.CalledProcessError as e:
                print(f"Failed to create venv: {e}", file=sys.stderr)
                return False

        # Determine python executable
        if sys.platform == "win32":
            python_exe = venv_path / "Scripts" / "python.exe"
        else:
            python_exe = venv_path / "bin" / "python"

        # Upgrade pip and install requirements
        print("Installing Python dependencies...")
        try:
            subprocess.run([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"], check=True)
            subprocess.run([str(python_exe), "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements.txt")], check=True)
            if (PROJECT_ROOT / "requirements-ml.txt").exists():
                subprocess.run([str(python_exe), "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements-ml.txt")], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Failed to install Python dependencies: {e}", file=sys.stderr)
            return False

        # Check Node.js and build dashboard
        print("Building dashboard...")
        try:
            # Check if npm exists
            subprocess.run(["npm", "--version"], check=True, capture_output=True)
            web_dir = PROJECT_ROOT / "web"
            if not (web_dir / "node_modules").exists():
                print("Installing Node.js dependencies...")
                subprocess.run(["npm", "install"], cwd=str(web_dir), check=True)
            print("Building Next.js dashboard...")
            subprocess.run(["npm", "run", "build"], cwd=str(web_dir), check=True)
        except subprocess.CalledProcessError as e:
            print(f"Failed to build dashboard: {e}", file=sys.stderr)
            print("Dashboard build failed but continuing setup...")
            return False
        except FileNotFoundError:
            print("Node.js not found. Please install Node.js 20.9+ to build the dashboard.", file=sys.stderr)
            return False

        # Create .env from .env.example if not exists
        env_file = PROJECT_ROOT / ".env"
        env_example = PROJECT_ROOT / ".env.example"
        if not env_file.exists() and env_example.exists():
            import shutil
            shutil.copy2(env_example, env_file)
            print("Created .env from .env.example. Add your GROQ_API_KEY and Webull credentials.")

        print("Setup complete!")
        return True

    def auth(self) -> bool:
        """Run Webull authentication."""
        import subprocess
        from pathlib import Path

        print("Running Webull authentication...")
        try:
            if sys.platform == "win32":
                python_exe = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
            else:
                python_exe = PROJECT_ROOT / ".venv" / "bin" / "python"

            subprocess.run([str(python_exe), "-m", "webull_openapi_mcp", "auth", "--env-file", str(PROJECT_ROOT / ".env")], check=True)
            print("Authentication complete.")
            return True
        except subprocess.CalledProcessError as e:
            print(f"Authentication failed: {e}", file=sys.stderr)
            return False
        except FileNotFoundError:
            print("webull-openapi-mcp not installed. Run 'setup' first.", file=sys.stderr)
            return False

    def stop(self) -> bool:
        """Stop the trader: graceful API shutdown, then terminate, then kill.

        Only ever signals a PID that is verified to be the app (lock-file PID whose
        command line is the app entry point, or a strict process-scan match). Never
        signals this process or its parent.
        """
        print("Stopping AI Trader...")
        try:
            port = int(self.settings.port)
        except Exception:
            port = 8000
        data_dir = _data_dir()
        lock = read_lock_file(data_dir / "trader.lock")
        if lock is not None and lock[1] != port:
            print(f"Lock file is for port {lock[1]}, using that port.")
            port = lock[1]

        # 1) graceful shutdown via the API
        try:
            import requests
            headers = {"X-Trader": "1"}
            token = read_api_token(data_dir / "api_token")
            if token:
                headers["X-Trader-Token"] = token
            requests.post(f"http://127.0.0.1:{port}/api/shutdown", headers=headers, timeout=10)
            print("Shutdown requested via API.")
        except Exception:
            print("The app did not answer on its port (it may already be stopped).")

        own = {os.getpid(), os.getppid()}
        procs = list_processes()

        # 2) the lock-file PID, verified
        targets: list[int] = []
        if lock is not None and lock[0] not in own and self._is_pid_alive(lock[0]):
            cmd = next((c for p, _pp, c in procs if p == lock[0]), None)
            if cmd is not None and is_app_cmdline(cmd, PROJECT_ROOT, require_repo=False):
                print(f"Found lock file: PID {lock[0]} on port {lock[1]}")
                targets.append(lock[0])
            else:
                print(f"Lock-file PID {lock[0]} is alive but is not the app; leaving it alone.")

        # 3) fallback scan (exact entry point + repo path, never ourselves)
        for pid in find_app_pids(procs, PROJECT_ROOT, own):
            if pid not in targets:
                print(f"Found app process PID {pid}")
                targets.append(pid)

        if not targets:
            print("No running app process found. Stopped.")
            return True

        ok = True
        for pid in targets:
            ok = self._stop_pid(pid) and ok
        print("Stopped." if ok else "Some processes could not be stopped.")
        return ok

    def _stop_pid(self, pid: int, grace: float = 20.0, term_wait: float = 10.0) -> bool:
        """Wait for a graceful exit, then terminate, then kill."""
        if self._wait_gone(pid, grace):
            print(f"PID {pid} exited gracefully.")
            return True
        print(f"PID {pid} still running after {grace:.0f}s: terminating.")
        self._signal_pid(pid, force=False)
        if self._wait_gone(pid, term_wait):
            return True
        print(f"PID {pid} ignored terminate: killing.")
        self._signal_pid(pid, force=True)
        return self._wait_gone(pid, 5.0)

    def _wait_gone(self, pid: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while True:
            if not self._is_pid_alive(pid):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    def _signal_pid(self, pid: int, force: bool) -> None:
        try:
            if sys.platform == "win32":
                cmd = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
                subprocess.run(cmd, capture_output=True, timeout=15)
            else:
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except Exception as e:
            print(f"Could not signal PID {pid}: {e}", file=sys.stderr)

    def _is_pid_alive(self, pid: int) -> bool:
        """Check if a PID is alive."""
        if sys.platform == "win32":
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=10
                )
                return pid_in_tasklist_csv(result.stdout, pid)
            except Exception:
                return False
        else:
            try:
                os.kill(pid, 0)
                return True
            except PermissionError:
                return True
            except (OSError, ProcessLookupError):
                return False

    # --- Autostart management ---

    def install_autostart(self) -> bool:
        """Install autostart for the current platform."""
        if sys.platform == "win32":
            return self._install_windows_autostart()
        elif sys.platform == "darwin":
            return self._install_macos_autostart()
        else:
            return self._install_linux_autostart()

    def uninstall_autostart(self) -> bool:
        """Remove autostart for the current platform."""
        if sys.platform == "win32":
            return self._uninstall_windows_autostart()
        elif sys.platform == "darwin":
            return self._uninstall_macos_autostart()
        else:
            return self._uninstall_linux_autostart()

    def check_autostart_status(self) -> dict:
        """Check if autostart is installed."""
        if sys.platform == "win32":
            return self._check_windows_autostart()
        elif sys.platform == "darwin":
            return self._check_macos_autostart()
        else:
            return self._check_linux_autostart()

    # Windows autostart (Task Scheduler)
    def _install_windows_autostart(self) -> bool:
        """Install Windows autostart via Task Scheduler."""
        try:
            python_exe = sys.executable
            script_path = PROJECT_ROOT / "scripts" / "trader.py"
            task_name = "AITrader"

            # Create task that runs at logon with highest privileges
            cmd = [
                "schtasks", "/Create", "/TN", task_name,
                "/TR", f'"{python_exe}" "{script_path}"',
                "/SC", "ONLOGON", "/RL", "HIGHEST", "/F",
                "/IT",  # Run only if network available
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                print(f"Autostart installed: {task_name}")
                return True
            else:
                print(f"Failed to install autostart: {result.stderr}", file=sys.stderr)
                return False
        except Exception as e:
            print(f"Error installing autostart: {e}", file=sys.stderr)
            return False

    def _uninstall_windows_autostart(self) -> bool:
        """Remove Windows autostart task."""
        try:
            task_name = "AITrader"
            cmd = ["schtasks", "/Delete", "/TN", task_name, "/F"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                print("Autostart removed")
                return True
            else:
                print(f"Failed to remove autostart: {result.stderr}", file=sys.stderr)
                return False
        except Exception as e:
            print(f"Error removing autostart: {e}", file=sys.stderr)
            return False

    def _check_windows_autostart(self) -> dict:
        """Check Windows autostart status."""
        try:
            task_name = "AITrader"
            cmd = ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            installed = result.returncode == 0
            return {"installed": installed, "method": "Task Scheduler", "details": result.stdout if installed else "Not found"}
        except Exception as e:
            return {"installed": False, "method": "Task Scheduler", "details": str(e)}

    # macOS autostart (launchd)
    def _install_macos_autostart(self) -> bool:
        """Install macOS autostart via launchd."""
        try:
            python_exe = sys.executable
            script_path = PROJECT_ROOT / "scripts" / "trader.py"
            plist_path = Path.home() / "Library" / "LaunchAgents" / "com.user.ai-trader.plist"

            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.ai-trader</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe}</string>
        <string>{script_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>{_data_dir()}/trader.out.log</string>
    <key>StandardErrorPath</key>
    <string>{_data_dir()}/trader.err.log</string>
    <key>WorkingDirectory</key>
    <string>{PROJECT_ROOT}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>"""

            plist_path.parent.mkdir(parents=True, exist_ok=True)
            plist_path.write_text(plist_content)

            # Load the agent
            result = subprocess.run(
                ["launchctl", "load", str(plist_path)],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                print(f"Autostart installed: {plist_path}")
                return True
            else:
                print(f"Failed to load launch agent: {result.stderr}", file=sys.stderr)
                return False
        except Exception as e:
            print(f"Error installing autostart: {e}", file=sys.stderr)
            return False

    def _uninstall_macos_autostart(self) -> bool:
        """Remove macOS autostart."""
        try:
            plist_path = Path.home() / "Library" / "LaunchAgents" / "com.user.ai-trader.plist"
            if plist_path.exists():
                subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, timeout=10)
                plist_path.unlink()
                print("Autostart removed")
                return True
            else:
                print("Autostart not found")
                return True
        except Exception as e:
            print(f"Error removing autostart: {e}", file=sys.stderr)
            return False

    def _check_macos_autostart(self) -> dict:
        """Check macOS autostart status."""
        try:
            plist_path = Path.home() / "Library" / "LaunchAgents" / "com.user.ai-trader.plist"
            installed = plist_path.exists()
            if installed:
                result = subprocess.run(
                    ["launchctl", "list", "com.user.ai-trader"],
                    capture_output=True, text=True, timeout=5
                )
                return {"installed": True, "method": "launchd", "details": result.stdout}
            return {"installed": False, "method": "launchd", "details": "Plist not found"}
        except Exception as e:
            return {"installed": False, "method": "launchd", "details": str(e)}

    # Linux autostart (systemd user service)
    def _install_linux_autostart(self) -> bool:
        """Install Linux autostart via systemd user service."""
        try:
            python_exe = sys.executable
            script_path = PROJECT_ROOT / "scripts" / "trader.py"
            service_path = Path.home() / ".config" / "systemd" / "user" / "ai-trader.service"

            service_content = f"""[Unit]
Description=AI Trader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python_exe} {script_path}
WorkingDirectory={PROJECT_ROOT}
Restart=on-failure
RestartSec=10
StandardOutput=append:{_data_dir()}/trader.out.log
StandardError=append:{_data_dir()}/trader.err.log
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
"""

            service_path.parent.mkdir(parents=True, exist_ok=True)
            service_path.write_text(service_content)

            # Enable and start
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
            result = subprocess.run(["systemctl", "--user", "enable", "--now", "ai-trader"], capture_output=True, text=True, timeout=10)

            if result.returncode == 0:
                print(f"Autostart installed: {service_path}")
                return True
            else:
                print(f"Failed to enable service: {result.stderr}", file=sys.stderr)
                return False
        except Exception as e:
            print(f"Error installing autostart: {e}", file=sys.stderr)
            return False

    def _uninstall_linux_autostart(self) -> bool:
        """Remove Linux autostart."""
        try:
            subprocess.run(["systemctl", "--user", "disable", "--now", "ai-trader"], capture_output=True, timeout=10)
            service_path = Path.home() / ".config" / "systemd" / "user" / "ai-trader.service"
            if service_path.exists():
                service_path.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
            print("Autostart removed")
            return True
        except Exception as e:
            print(f"Error removing autostart: {e}", file=sys.stderr)
            return False

    def _check_linux_autostart(self) -> dict:
        """Check Linux autostart status."""
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-enabled", "ai-trader"],
                capture_output=True, text=True, timeout=5
            )
            enabled = result.returncode == 0 and result.stdout.strip() == "enabled"

            status_result = subprocess.run(
                ["systemctl", "--user", "status", "ai-trader"],
                capture_output=True, text=True, timeout=5
            )

            return {
                "installed": enabled,
                "method": "systemd user service",
                "details": status_result.stdout if enabled else "Not enabled"
            }
        except Exception as e:
            return {"installed": False, "method": "systemd user service", "details": str(e)}


def main():
    parser = argparse.ArgumentParser(description="AI Trader cross-platform launcher")
    subparsers = parser.add_subparsers(dest="command", help="Subcommands")

    # Default run command
    run_parser = subparsers.add_parser("run", help="Run the trader (default)")
    run_parser.add_argument("--no-sleep-prevent", action="store_true", help="Disable sleep prevention")

    # Setup command
    setup_parser = subparsers.add_parser("setup", help="Install dependencies and build dashboard")

    # Install autostart
    install_parser = subparsers.add_parser("install", help="Install autostart")
    install_parser.add_argument("--no-sleep-prevent", action="store_true", help="Disable sleep prevention")

    # Uninstall autostart
    uninstall_parser = subparsers.add_parser("uninstall", help="Remove autostart")

    # Status
    status_parser = subparsers.add_parser("status", help="Check autostart status")

    # Auth
    auth_parser = subparsers.add_parser("auth", help="Run Webull authentication")

    # Stop
    stop_parser = subparsers.add_parser("stop", help="Gracefully stop the trader")

    args = parser.parse_args()

    # Default to 'run' if no command given
    if args.command is None:
        args.command = "run"

    launcher = TraderLauncher()

    if getattr(args, 'no_sleep_prevent', False):
        launcher.sleep_prevent_enabled = False

    if args.command == "setup":
        success = launcher.setup()
        sys.exit(0 if success else 1)

    if args.command == "install":
        success = launcher.install_autostart()
        sys.exit(0 if success else 1)

    if args.command == "uninstall":
        success = launcher.uninstall_autostart()
        sys.exit(0 if success else 1)

    if args.command == "status":
        status = launcher.check_autostart_status()
        print(f"Autostart: {'Installed' if status['installed'] else 'Not installed'}")
        print(f"Method: {status['method']}")
        print(f"Details: {status['details']}")
        sys.exit(0 if status['installed'] else 1)

    if args.command == "auth":
        success = launcher.auth()
        sys.exit(0 if success else 1)

    if args.command == "stop":
        success = launcher.stop()
        sys.exit(0 if success else 1)

    # Normal run
    sys.exit(launcher.run())


if __name__ == "__main__":
    main()