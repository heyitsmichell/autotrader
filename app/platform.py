"""Cross-platform utilities for single-instance guard, notifications, sleep prevention."""

from __future__ import annotations

import os
import sys
import time
import signal
import subprocess
from pathlib import Path
from typing import Optional

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is still alive."""
    if pid <= 0:
        return False
    try:
        if IS_WINDOWS:
            # Use tasklist to check if process exists
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            return str(pid) in result.stdout
        else:
            # POSIX: send signal 0 to check existence
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        return False
    except Exception:
        return False


def get_process_using_port(port: int, host: str = "127.0.0.1") -> Optional[int]:
    """Find the PID of the process listening on the given port."""
    try:
        if IS_WINDOWS:
            # Use netstat to find process using port
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        try:
                            pid = int(parts[-1])
                            if pid > 0 and is_process_alive(pid):
                                return pid
                        except ValueError:
                            pass
            return None
        else:
            # Use lsof or ss on POSIX. Only LISTEN sockets count: `lsof -ti tcp:8000`
            # also returns every browser that merely has a connection to the app, which
            # used to make a clean restart look like "another instance is running".
            for cmd in [
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                ["ss", "-ltnp", f"sport = :{port}"],
            ]:
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    if result.stdout.strip():
                        for line in result.stdout.strip().splitlines():
                            if "LISTEN" in line or line.strip().isdigit():
                                parts = line.split()
                                for part in parts:
                                    if part.isdigit():
                                        pid = int(part)
                                        if is_process_alive(pid):
                                            return pid
                except FileNotFoundError:
                    continue
            return None
    except Exception:
        return None


class SingleInstanceLock:
    """
    Cross-platform single-instance lock using a PID file with port probe.

    Works on Windows, macOS, and Linux without requiring fcntl/msvcrt.
    Handles stale lock files from crashed processes.
    """

    def __init__(self, lock_file: Path, port: int, host: str = "127.0.0.1"):
        self.lock_file = lock_file
        self.port = port
        self.host = host
        self._acquired = False

    def acquire(self) -> None:
        """Acquire the lock. Raises SystemExit(3) if another instance is running."""
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

        # Check for existing lock file
        if self.lock_file.exists():
            stale = self._check_and_clean_stale_lock()
            if not stale:
                # Lock file is valid and process is alive
                self._exit_with_error(3)

        # Check if port is actually in use (handles case where lock file was deleted but process still runs)
        port_pid = get_process_using_port(self.port, self.host)
        if port_pid is not None and port_pid != os.getpid():
            self._exit_with_error(3, port_pid)

        # Create lock file with our PID and port
        try:
            self.lock_file.write_text(f"{os.getpid()}:{self.port}")
            self._acquired = True
            # Clear any stale duplicate instance flag on successful acquisition
            self._clear_duplicate_flag()
        except Exception as e:
            raise SystemExit(f"Failed to create lock file: {e}")

    def _check_and_clean_stale_lock(self) -> bool:
        """Check if existing lock file is stale (process dead). Returns True if cleaned up."""
        try:
            content = self.lock_file.read_text().strip()
            parts = content.split(":")
            if len(parts) == 2:
                pid = int(parts[0])
                port = int(parts[1])

                # Check if the process is still alive
                if not is_process_alive(pid):
                    # Process is dead - check if port is still in use by someone else
                    port_pid = get_process_using_port(port, self.host)
                    if port_pid is None or port_pid == pid:
                        # Port is free or held by the dead process - safe to remove lock
                        self.lock_file.unlink(missing_ok=True)
                        return True
                    else:
                        # Port is held by a different process - that's the real owner
                        self._exit_with_error(3, port_pid)
        except Exception:
            # Any error reading/parsing - treat as stale and remove
            try:
                self.lock_file.unlink(missing_ok=True)
            except Exception:
                pass
            return True
        return False

    def _exit_with_error(self, code: int, existing_pid: Optional[int] = None) -> None:
        """Exit with the given code and informative message."""
        if existing_pid:
            msg = f"AI Trader is already running (PID {existing_pid} on port {self.port}). Stop it first."
        else:
            try:
                content = self.lock_file.read_text().strip()
                parts = content.split(":")
                if len(parts) == 2:
                    pid = parts[0]
                    msg = f"AI Trader is already running (PID {pid} on port {self.port}). Stop it first."
                else:
                    msg = f"AI Trader is already running on port {self.port}. Stop it first."
            except Exception:
                msg = f"AI Trader is already running on port {self.port}. Stop it first."

        # Write flag file for dashboard to show duplicate instance warning
        self._write_duplicate_flag(msg)

        # Print message to stderr and exit with the specified code
        import sys
        sys.stderr.write(msg + "\n")
        sys.exit(code)

    def _write_duplicate_flag(self, message: str) -> None:
        """Write a flag file that the dashboard reads to show a warning banner."""
        try:
            flag_file = self.lock_file.parent / "duplicate_instance.flag"
            flag_file.write_text(f"{int(time.time())}:{message}")
        except Exception:
            pass  # Best effort only

    def _clear_duplicate_flag(self) -> None:
        """Clear the duplicate instance flag file on successful startup."""
        try:
            flag_file = self.lock_file.parent / "duplicate_instance.flag"
            flag_file.unlink(missing_ok=True)
        except Exception:
            pass  # Best effort only

    def release(self) -> None:
        """Release the lock (called on normal exit)."""
        if self._acquired:
            try:
                if self.lock_file.exists():
                    content = self.lock_file.read_text().strip()
                    if content.startswith(f"{os.getpid()}:"):
                        self.lock_file.unlink(missing_ok=True)
            except Exception:
                pass
            self._acquired = False


def acquire_single_instance_lock(lock_file: Path, port: int, host: str = "127.0.0.1") -> SingleInstanceLock:
    """
    Acquire a single-instance lock. Returns the lock object (must call release() on exit).
    Raises SystemExit(3) if another instance is running.
    """
    lock = SingleInstanceLock(lock_file, port, host)
    lock.acquire()
    return lock


# --- Notification backends (Step 1.7) ---

def send_desktop_notification(title: str, message: str, urgent: bool = False) -> bool:
    """
    Send a local desktop notification.
    Returns True if sent successfully, False if unavailable.
    Never raises - fails silently.
    """
    try:
        if IS_WINDOWS:
            # Windows 10/11 toast via PowerShell
            ps_script = f'''
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            $template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
            $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
            $text = $xml.GetElementsByTagName("text")
            $text[0].AppendChild($xml.CreateTextNode("{title}")) | Out-Null
            $text[1].AppendChild($xml.CreateTextNode("{message}")) | Out-Null
            $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("AI Trader").Show($toast)
            '''
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, timeout=5
            )
            return True

        elif IS_MACOS:
            # macOS notification via osascript
            # Escape quotes and backslashes for AppleScript
            safe_title = title.replace('"', '\\"').replace('\\', '\\\\')
            safe_msg = message.replace('"', '\\"').replace('\\', '\\\\')
            script = f'display notification "{safe_msg}" with title "{safe_title}"'
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5
            )
            return True

        else:
            # Linux: try notify-send
            subprocess.run(
                ["notify-send", "--urgency", "critical" if urgent else "normal", title, message],
                capture_output=True, timeout=5
            )
            return True
    except Exception:
        return False


# --- Sleep prevention (Step 1.8) ---

class SleepPreventer:
    """Prevent system sleep while trading is active."""

    def __init__(self):
        self._active = False
        self._caffeinate_pid = None

    def enable(self) -> bool:
        """Enable sleep prevention. Returns True if successful."""
        if self._active:
            return True

        try:
            if IS_WINDOWS:
                # Windows: SetThreadExecutionState
                import ctypes
                ES_CONTINUOUS = 0x80000000
                ES_SYSTEM_REQUIRED = 0x00000001
                ES_DISPLAY_REQUIRED = 0x00000002
                result = ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                )
                self._active = result != 0
                return self._active

            elif IS_MACOS:
                # macOS: caffeinate -i -w <pid>
                self._caffeinate_pid = subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                ).pid
                self._active = True
                return True

            else:
                # Linux: systemd-inhibit or similar
                # For now, just return False (not implemented)
                return False
        except Exception:
            return False

    def disable(self) -> None:
        """Disable sleep prevention."""
        if not self._active:
            return

        try:
            if IS_WINDOWS:
                import ctypes
                ES_CONTINUOUS = 0x80000000
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            elif IS_MACOS and self._caffeinate_pid:
                os.kill(self._caffeinate_pid, signal.SIGTERM)
        except Exception:
            pass
        finally:
            self._active = False
            self._caffeinate_pid = None

    @property
    def is_active(self) -> bool:
        return self._active
