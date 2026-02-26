"""
v13pro/supervisor.py -- Process supervisor.

Runs the bot as a subprocess with:
  - Heartbeat monitoring (restarts if stale)
  - Anti-flap backoff (avoids restart loops)
  - Log watching (classifies errors in real-time)
  - Position guard (only restart when safe)
  - PID lockfile
  - Incident logging (JSONL)

Adapted from obr/supervisor.py for v13pro.
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Self-contained config for supervisor (no v13pro imports needed)
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
STATE_FILE = BASE_DIR / "state.json"
PID_FILE = BASE_DIR / "supervisor.pid"
INCIDENT_LOG = LOG_DIR / "incidents.jsonl"

# Timings
HEARTBEAT_STALE_SECS = 600     # 10 min no activity = dead
POLL_INTERVAL = 30              # check every 30s
STABLE_RUN_SECS = 120           # must run 2min to count as stable
MAX_RESTART_PER_HOUR = 6

# Backoff schedule
BACKOFF_SCHEDULE = [10, 30, 60, 120, 300]

# Error patterns
FATAL_PATTERNS = [
    "authenticationerror", "invalid api key", "api key expired",
    "permission denied", "account banned", "insufficient balance",
    "account suspended",
]
TRANSIENT_PATTERNS = [
    "ratelimit", "networkerror", "timeout", "connection reset",
    "service unavailable", "502", "503", "429",
]


class LogWatcher:
    """Tail the bot log file, classify errors in real-time."""

    def __init__(self, log_dir: Path):
        self._log_dir = log_dir
        self._file = None
        self._pos = 0
        self._errors = []
        self._fatal = False
        self._current_path = None

    def update(self):
        """Read new lines from log file."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._log_dir / f"v13pro_{today}.log"

        if path != self._current_path:
            self._current_path = path
            self._pos = 0
            if self._file:
                self._file.close()
                self._file = None

        if not path.exists():
            return

        try:
            if self._file is None:
                self._file = open(path, "r", encoding="utf-8", errors="replace")
                self._file.seek(0, 2)  # end of file
                self._pos = self._file.tell()
                return

            self._file.seek(self._pos)
            for line in self._file:
                lower = line.lower()
                for pat in FATAL_PATTERNS:
                    if pat in lower:
                        self._fatal = True
                        self._errors.append(("FATAL", line.strip()))
                for pat in TRANSIENT_PATTERNS:
                    if pat in lower:
                        self._errors.append(("TRANSIENT", line.strip()))
            self._pos = self._file.tell()
        except Exception:
            pass

    @property
    def has_fatal(self):
        return self._fatal

    def reset(self):
        self._errors.clear()
        self._fatal = False

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


def _log_incident(event: str, details: dict = None):
    """Append incident to JSONL log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **(details or {}),
    }
    try:
        with open(INCIDENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _has_open_positions() -> bool:
    """Check state.json for pending entries."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            pending = state.get("pending_entries", [])
            return len(pending) > 0
    except Exception:
        pass
    return False


def _write_pid():
    PID_FILE.write_text(str(os.getpid()))


def _remove_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


class Supervisor:
    """Process supervisor for v13pro bot."""

    def __init__(self, bot_args: list = None):
        self._bot_args = bot_args or []
        self._process = None
        self._running = False
        self._restart_count = 0
        self._restart_times = []
        self._backoff_idx = 0
        self._watcher = LogWatcher(LOG_DIR)

    def run(self):
        """Run supervisor loop (blocking)."""
        _write_pid()
        self._running = True
        print(f"[Supervisor] Starting — PID {os.getpid()}")
        _log_incident("supervisor_start")

        # Handle SIGINT/SIGTERM
        def _handle_signal(signum, frame):
            print(f"\n[Supervisor] Signal {signum} — shutting down")
            self._running = False
            self._stop_bot()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        try:
            while self._running:
                self._start_bot()
                self._monitor_bot()

                if not self._running:
                    break

                # Check if we should restart
                if self._watcher.has_fatal:
                    print("[Supervisor] FATAL error detected — NOT restarting")
                    _log_incident("fatal_error_stop")
                    break

                # Rate limit restarts
                now = time.time()
                self._restart_times = [t for t in self._restart_times
                                       if now - t < 3600]
                if len(self._restart_times) >= MAX_RESTART_PER_HOUR:
                    print("[Supervisor] Too many restarts (>6/hour) — stopping")
                    _log_incident("restart_limit_reached")
                    break

                # Backoff delay
                delay = BACKOFF_SCHEDULE[min(self._backoff_idx,
                                             len(BACKOFF_SCHEDULE) - 1)]

                # Position guard: if open positions, restart faster
                if _has_open_positions():
                    delay = min(delay, 10)
                    print(f"[Supervisor] Open positions — fast restart in {delay}s")
                else:
                    print(f"[Supervisor] Restarting in {delay}s "
                          f"(attempt {self._restart_count + 1})")

                _log_incident("restart_scheduled", {"delay": delay})
                time.sleep(delay)

                self._restart_count += 1
                self._restart_times.append(time.time())
                self._backoff_idx += 1

        finally:
            _remove_pid()
            self._watcher.close()
            print("[Supervisor] Exited")

    def _start_bot(self):
        """Start bot as subprocess."""
        cmd = [sys.executable, "-m", "v13pro.run"] + self._bot_args
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_path = LOG_DIR / f"v13pro_{today}.log"

        self._log_file = open(log_path, "a", encoding="utf-8")
        self._process = subprocess.Popen(
            cmd,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(BASE_DIR.parent),  # workspace root
        )
        self._start_time = time.time()
        print(f"[Supervisor] Bot started — PID {self._process.pid}")
        _log_incident("bot_started", {"pid": self._process.pid})
        self._watcher.reset()

    def _stop_bot(self):
        """Stop bot subprocess."""
        if self._process and self._process.poll() is None:
            print("[Supervisor] Stopping bot...")
            try:
                self._process.terminate()
                self._process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
            _log_incident("bot_stopped")

        if hasattr(self, "_log_file") and self._log_file:
            self._log_file.close()

    def _monitor_bot(self):
        """Monitor bot process until it exits or goes stale."""
        last_activity = time.time()

        while self._running:
            # Check if process exited
            ret = self._process.poll()
            if ret is not None:
                uptime = time.time() - self._start_time
                print(f"[Supervisor] Bot exited code={ret} "
                      f"(uptime={uptime:.0f}s)")
                _log_incident("bot_exited", {
                    "code": ret, "uptime_s": uptime})

                if uptime >= STABLE_RUN_SECS:
                    self._backoff_idx = 0  # reset backoff on stable run
                return

            # Watch logs
            self._watcher.update()
            if self._watcher.has_fatal:
                self._stop_bot()
                return

            # Check heartbeat (state.json mtime)
            try:
                if STATE_FILE.exists():
                    mtime = STATE_FILE.stat().st_mtime
                    if time.time() - mtime < HEARTBEAT_STALE_SECS:
                        last_activity = time.time()
            except Exception:
                pass

            # Stale check
            if time.time() - last_activity > HEARTBEAT_STALE_SECS:
                print(f"[Supervisor] Bot stale for {HEARTBEAT_STALE_SECS}s")
                _log_incident("bot_stale")
                self._stop_bot()
                return

            time.sleep(POLL_INTERVAL)
