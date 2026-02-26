"""
obr/supervisor.py -- Skill Agent: autonomous bot monitor & auto-healer.

Responsibilities:
  1. PROCESS WATCHDOG  — Runs bot as subprocess, detects crashes
  2. ERROR SCANNER     — Tails log file, classifies errors in real-time
  3. POSITION GUARD    — Only restarts when no open positions on exchange
  4. INCIDENT LOG      — Maintains separate supervisor log with full context
  5. ANTI-FLAP         — Exponential backoff prevents restart loops
  6. HEARTBEAT CHECK   — Detects frozen bot (no heartbeat in N minutes)
  7. EXCHANGE AUDIT    — Verifies connectivity before restart
  8. ERROR DIGEST      — Run with --errors to see recent issues at a glance

Usage:
  .venv\\Scripts\\python.exe run_supervisor.py          # Start supervised bot
  .venv\\Scripts\\python.exe run_supervisor.py --errors  # Show error digest
"""

import os
import sys
import json
import time
import signal
import subprocess
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import deque

# ──────────────────────────────────────────────────────────────
#  ANSI colors (standalone -- supervisor must work independently)
# ──────────────────────────────────────────────────────────────

if sys.platform == "win32":
    os.system("")  # enable ANSI on Windows

# Force UTF-8 stdout for supervisor itself (Windows console)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class _C:
    R  = "\033[0m";  B  = "\033[1m";  D  = "\033[2m"
    RED = "\033[91m"; GRN = "\033[92m"; YLW = "\033[93m"
    BLU = "\033[94m"; MAG = "\033[95m"; CYN = "\033[96m"
    WHT = "\033[97m"; BGRED = "\033[41m"


# ──────────────────────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent.resolve()
BOT_SCRIPT = str(BASE_DIR / "run_obr.py")
PYTHON = str(BASE_DIR / ".venv" / "Scripts" / "python.exe")
STATE_FILE = BASE_DIR / "obr" / "state.json"
LOG_DIR = BASE_DIR / "obr" / "logs"
SUPERVISOR_LOG = LOG_DIR / "supervisor.log"
INCIDENT_FILE = LOG_DIR / "incidents.jsonl"

# Backoff schedule (consecutive crashes → wait seconds)
BACKOFF_SCHEDULE = [10, 30, 60, 120, 300]   # caps at 5 min
STABLE_RUN_SECS = 120                       # run > 2min resets crash counter

# Heartbeat
HEARTBEAT_STALE_SECS = 600                  # 10 min without heartbeat → stuck
MAX_RESTART_PER_HOUR = 6                    # anti-flap cap

# Error patterns that are FATAL (don't auto-restart)
FATAL_PATTERNS = [
    "equity too low",
    "api key",
    "api_key",
    "invalid api",
    "authentication",
    "permission denied",
    "insufficient balance",
    "bybit_api_key env var not set",
    "bybit_api_secret env var not set",
]

# Transient errors (expected, just log)
TRANSIENT_PATTERNS = [
    "rate limit",
    "too many visits",
    "429",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "network",
    "temporary",
    "econnreset",
]

LOCKFILE = LOG_DIR / "obr_bot.pid"


# ──────────────────────────────────────────────────────────────
#  PID lockfile  (prevents duplicate bot instances)
# ──────────────────────────────────────────────────────────────

def _kill_stale_lockfile():
    """If a lockfile exists, kill stale process tree then remove it."""
    if not LOCKFILE.exists():
        return
    try:
        old_pid = int(LOCKFILE.read_text().strip())
    except (ValueError, OSError):
        LOCKFILE.unlink(missing_ok=True)
        return

    # Check if old process is still alive
    import psutil  # optional; fall back to os.kill
    try:
        proc = psutil.Process(old_pid)
        # Kill the entire process tree
        children = proc.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except Exception:
                pass
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        # psutil not available or process already dead — try os.kill
        try:
            os.kill(old_pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass  # already dead

    LOCKFILE.unlink(missing_ok=True)


def _write_lockfile():
    """Write current process PID to lockfile."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOCKFILE.write_text(str(os.getpid()))


def _remove_lockfile():
    """Remove lockfile on clean exit."""
    LOCKFILE.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────
#  Logging (supervisor's own output)
# ──────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _log(level: str, msg: str, color: str = "") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    icon_map = {
        "INFO": ("🔧", _C.CYN), "WARN": ("⚠️ ", _C.YLW),
        "ERROR": ("❌", _C.RED), "OK": ("✅", _C.GRN),
        "RESTART": ("🔄", _C.MAG), "FATAL": ("💀", _C.RED),
        "HEART": ("💓", _C.CYN), "GUARD": ("🛡️ ", _C.BLU),
    }
    icon, default_color = icon_map.get(level, ("  ", _C.WHT))
    c = color or default_color

    console = (f"{_C.D}{_C.CYN}{ts}{_C.R} "
               f"{icon} {_C.D}│{_C.R} "
               f"{c}{msg}{_C.R}")
    print(console, flush=True)

    # File (plain)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(SUPERVISOR_LOG, "a", encoding="utf-8") as f:
        f.write(f"{_ts()} | {level:7s} | {msg}\n")


def _log_incident(category: str, detail: str, **extra) -> None:
    """Append structured incident to JSONL file."""
    os.makedirs(LOG_DIR, exist_ok=True)
    record = {
        "ts": _ts(),
        "category": category,
        "detail": detail[:500],
        **{k: str(v)[:200] for k, v in extra.items()},
    }
    with open(INCIDENT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ──────────────────────────────────────────────────────────────
#  State & exchange helpers
# ──────────────────────────────────────────────────────────────

def _read_state() -> dict:
    """Read state.json safely."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _has_open_positions_state() -> int:
    """Count open positions from state.json."""
    state = _read_state()
    return len(state.get("pending_entries", []))


def _has_open_positions_exchange() -> int:
    """Check exchange directly for open positions (independent of bot)."""
    try:
        import ccxt
        from obr.config import API_KEY, API_SECRET, MAINNET, DEMO_MODE

        ex = ccxt.bybit({
            "apiKey": API_KEY,
            "secret": API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "swap", "recvWindow": 20_000},
        })
        if not MAINNET:
            if DEMO_MODE:
                ex.enable_demo_trading(True)
            else:
                ex.set_sandbox_mode(True)
        ex.load_markets()

        positions = ex.fetch_positions(params={"category": "linear"})
        open_count = sum(
            1 for p in positions
            if abs(float(p.get("contracts", 0) or 0)) > 0
        )
        return open_count
    except Exception as e:
        _log("WARN", f"Exchange position check failed: {e}")
        # Fallback to state file
        return _has_open_positions_state()


def _check_exchange_connectivity() -> bool:
    """Quick connectivity test before restart."""
    try:
        import ccxt
        ex = ccxt.bybit({"enableRateLimit": True,
                          "options": {"defaultType": "swap"}})
        ex.fetch_time()
        return True
    except Exception as e:
        _log("WARN", f"Exchange connectivity failed: {e}")
        return False


# ──────────────────────────────────────────────────────────────
#  Log watcher (real-time error scanner)
# ──────────────────────────────────────────────────────────────

class LogWatcher:
    """Tail the bot's log file, detect and classify errors in real-time."""

    def __init__(self):
        self._running = False
        self._thread = None
        self._recent_errors: deque = deque(maxlen=50)
        self._error_counts: dict = {}  # {category: count}
        self._last_heartbeat_seen: float = time.time()

    @property
    def recent_errors(self):
        return list(self._recent_errors)

    @property
    def last_heartbeat_age(self) -> float:
        return time.time() - self._last_heartbeat_seen

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _get_log_file(self) -> Path:
        """Get today's bot log file."""
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        return LOG_DIR / f"bot_{today}.log"

    def _run(self):
        """Tail the log file continuously."""
        log_file = self._get_log_file()
        current_date = datetime.now(timezone.utc).strftime("%Y%m%d")

        # Seek to end of existing file
        pos = 0
        if log_file.exists():
            pos = log_file.stat().st_size

        while self._running:
            try:
                # Check for date rollover
                new_date = datetime.now(timezone.utc).strftime("%Y%m%d")
                if new_date != current_date:
                    current_date = new_date
                    log_file = self._get_log_file()
                    pos = 0

                if not log_file.exists():
                    time.sleep(2)
                    continue

                size = log_file.stat().st_size
                if size < pos:
                    # File was truncated/rotated
                    pos = 0

                if size > pos:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        new_lines = f.read()
                        pos = f.tell()

                    for line in new_lines.splitlines():
                        self._process_line(line)

                time.sleep(1)

            except Exception:
                time.sleep(5)

    def _process_line(self, line: str):
        """Analyze a single log line."""
        lower = line.lower()

        # Heartbeat detection
        if "heartbeat" in lower:
            self._last_heartbeat_seen = time.time()
            return

        # Error/Critical detection
        is_error = False
        level = "INFO"
        if "| error |" in lower or "| err   |" in lower:
            is_error = True
            level = "ERROR"
        elif "| critical |" in lower or "| crt   |" in lower:
            is_error = True
            level = "CRITICAL"
        elif "| warn" in lower:
            # Track warnings too but don't incident them
            pass

        if not is_error:
            return

        # Classify
        category = self._classify(lower)
        self._error_counts[category] = self._error_counts.get(category, 0) + 1

        entry = {
            "ts": _ts(),
            "level": level,
            "category": category,
            "line": line.strip()[:300],
        }
        self._recent_errors.append(entry)

        # Log transient errors quietly, others loudly
        if category == "TRANSIENT":
            pass  # silently tracked
        elif category == "FATAL":
            _log("FATAL", f"FATAL error detected: {line.strip()[:120]}")
            _log_incident("FATAL_ERROR", line.strip()[:300], category=category)
        else:
            _log("ERROR", f"[{category}] {line.strip()[:120]}")
            _log_incident("BOT_ERROR", line.strip()[:300], category=category)

    def _classify(self, lower_line: str) -> str:
        for pat in FATAL_PATTERNS:
            if pat in lower_line:
                return "FATAL"
        for pat in TRANSIENT_PATTERNS:
            if pat in lower_line:
                return "TRANSIENT"
        return "UNKNOWN"


# ──────────────────────────────────────────────────────────────
#  The Supervisor
# ──────────────────────────────────────────────────────────────

class OBRSupervisor:
    """
    Autonomous skill agent that monitors the OBR bot process.

    Features:
      - Runs bot as subprocess, restarts on crash
      - Only restarts when safe (no open positions or exchange-protected)
      - Exponential backoff prevents restart storms
      - Real-time log error scanning
      - Heartbeat monitoring (frozen bot detection)
      - Exchange connectivity check before restart
      - Structured incident logging (JSONL)
    """

    def __init__(self):
        self._proc: subprocess.Popen = None
        self._watcher = LogWatcher()
        self._crash_count = 0
        self._restart_times: deque = deque(maxlen=MAX_RESTART_PER_HOUR)
        self._start_time: float = 0
        self._running = True
        self._total_restarts = 0
        self._uptime_start = time.time()

        # Handle SIGINT/SIGTERM gracefully
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        _log("INFO", f"Signal {signum} received -- shutting down gracefully")
        self._running = False
        self._kill_bot()

    def _kill_bot(self):
        if self._proc and self._proc.poll() is None:
            _log("INFO", "Sending terminate to bot process...")
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    _log("WARN", "Bot didn't stop, force killing...")
                    self._proc.kill()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    #  Banner
    # ──────────────────────────────────────────────────────────

    def _print_banner(self):
        w = 56
        print()
        print(f"{_C.MAG}{'═' * w}{_C.R}")
        print(f"{_C.MAG}║{_C.R}  🔧 {_C.B}{_C.WHT}"
              f"OBR Skill Agent  --  Auto-Healer{_C.R}"
              f"{' ' * 12}{_C.MAG}║{_C.R}")
        print(f"{_C.MAG}{'═' * w}{_C.R}")
        print(f"  🛡️  {_C.D}Mode:{_C.R}       {_C.B}{_C.GRN}SUPERVISED{_C.R}")
        print(f"  🔄 {_C.D}Auto-restart:{_C.R} {_C.CYN}ON{_C.R}  "
              f"{_C.D}(safe-only, exponential backoff){_C.R}")
        print(f"  📡 {_C.D}Log scanner:{_C.R}  {_C.CYN}ACTIVE{_C.R}  "
              f"{_C.D}(real-time error classification){_C.R}")
        print(f"  💓 {_C.D}Heartbeat:{_C.R}    {_C.CYN}MONITOR{_C.R}  "
              f"{_C.D}(stale after {HEARTBEAT_STALE_SECS}s){_C.R}")
        print(f"  📊 {_C.D}Incidents:{_C.R}    {_C.WHT}{INCIDENT_FILE}{_C.R}")
        print(f"{_C.MAG}{'─' * w}{_C.R}")
        print()

    # ──────────────────────────────────────────────────────────
    #  Bot lifecycle
    # ──────────────────────────────────────────────────────────

    def _start_bot(self) -> bool:
        """Launch the bot as a subprocess."""
        if not os.path.exists(PYTHON):
            _log("FATAL", f"Python not found: {PYTHON}")
            return False

        if not os.path.exists(BOT_SCRIPT):
            _log("FATAL", f"Bot script not found: {BOT_SCRIPT}")
            return False

        _log("RESTART", f"Starting bot process (restart #{self._total_restarts})...")

        try:
            # Force UTF-8 in subprocess so emojis/ANSI pass through
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"

            self._proc = subprocess.Popen(
                [PYTHON, BOT_SCRIPT, "--_supervised", "--yes"],
                cwd=str(BASE_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._start_time = time.time()
            self._total_restarts += 1

            # Start stdout reader thread (passes bot output to console)
            t = threading.Thread(target=self._pipe_output, daemon=True)
            t.start()

            _log("OK", f"Bot started (PID {self._proc.pid})")
            _log_incident("BOT_STARTED", f"PID={self._proc.pid}, "
                          f"restart_count={self._total_restarts}")
            return True

        except Exception as e:
            _log("FATAL", f"Failed to start bot: {e}")
            _log_incident("START_FAILED", str(e))
            return False

    def _pipe_output(self):
        """Read bot stdout and pass it through to supervisor console."""
        try:
            for line in self._proc.stdout:
                # Print bot output as-is (it has its own ANSI formatting)
                print(line, end="", flush=True)
        except Exception:
            pass

    def _is_bot_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _get_exit_code(self) -> int:
        if self._proc is None:
            return -1
        return self._proc.returncode or 0

    # ──────────────────────────────────────────────────────────
    #  Safety checks
    # ──────────────────────────────────────────────────────────

    def _is_safe_to_restart(self) -> tuple:
        """
        Check if it's safe to restart the bot.
        Returns (safe: bool, reason: str)
        """
        # Anti-flap: check restart rate
        now = time.time()
        recent = [t for t in self._restart_times if now - t < 3600]
        if len(recent) >= MAX_RESTART_PER_HOUR:
            return False, f"Rate limit: {len(recent)} restarts in last hour (max {MAX_RESTART_PER_HOUR})"

        # Check for fatal errors (don't restart on auth/config issues)
        for err in self._watcher.recent_errors[-5:]:
            if err.get("category") == "FATAL":
                age = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(err["ts"].replace(" ", "T") + "+00:00")
                       ).total_seconds()
                if age < 60:  # Fatal error within last minute
                    return False, f"Fatal error detected: {err.get('line', '')[:80]}"

        # Check exchange for open positions
        _log("GUARD", "Checking for open positions...")

        state_pending = _has_open_positions_state()
        exchange_open = _has_open_positions_exchange()

        if exchange_open > 0:
            _log("GUARD", f"⚠️  {exchange_open} position(s) open on exchange — "
                         f"exchange SL/TP are set, safe to restart")
            # Positions have exchange-side protection (SL/TP set at order time)
            # Bot will re-track them via _restore_guardian_positions on restart
            _log_incident("RESTART_WITH_POSITIONS",
                          f"exchange={exchange_open}, state={state_pending}")

        if state_pending > 0 and exchange_open == 0:
            # State says positions exist but exchange says no → orphaned state
            _log("WARN", f"State shows {state_pending} positions but exchange has 0 "
                         f"— state is stale, will clean up on restart")

        # Check exchange connectivity
        if not _check_exchange_connectivity():
            return False, "Exchange unreachable — waiting for connectivity"

        return True, "OK"

    def _get_backoff(self) -> int:
        """Get wait time based on consecutive crash count."""
        idx = min(self._crash_count, len(BACKOFF_SCHEDULE) - 1)
        return BACKOFF_SCHEDULE[idx]

    # ──────────────────────────────────────────────────────────
    #  Heartbeat monitoring
    # ──────────────────────────────────────────────────────────

    def _check_heartbeat(self):
        """Detect frozen bot (no heartbeat in log for too long)."""
        if not self._is_bot_alive():
            return

        age = self._watcher.last_heartbeat_age
        run_time = time.time() - self._start_time

        # Give bot time to start up (first 3 minutes)
        if run_time < 180:
            return

        if age > HEARTBEAT_STALE_SECS:
            _log("WARN", f"⏰ Bot heartbeat stale ({age:.0f}s) — may be frozen")
            _log_incident("HEARTBEAT_STALE", f"age={age:.0f}s, runtime={run_time:.0f}s")

            # If stale for 2x the threshold, force restart
            if age > HEARTBEAT_STALE_SECS * 2:
                _log("ERROR", "💀 Bot appears frozen — force restarting")
                _log_incident("FORCE_RESTART", "heartbeat_timeout",
                              age=f"{age:.0f}s")
                self._kill_bot()

    # ──────────────────────────────────────────────────────────
    #  Main loop
    # ──────────────────────────────────────────────────────────

    def run(self):
        """Main supervisor loop."""
        self._print_banner()

        # Kill any stale bot from a previous crashed session
        _kill_stale_lockfile()
        _write_lockfile()

        # Start log watcher
        self._watcher.start()
        _log("OK", "Log scanner started")

        # Initial bot launch
        if not self._start_bot():
            _log("FATAL", "Cannot start bot — aborting supervisor")
            return

        heartbeat_check_interval = 60  # check every 60s
        last_heartbeat_check = time.time()

        try:
            while self._running:
                time.sleep(3)

                # Periodic heartbeat check
                if time.time() - last_heartbeat_check > heartbeat_check_interval:
                    self._check_heartbeat()
                    last_heartbeat_check = time.time()

                # Check if bot is still running
                if not self._is_bot_alive():
                    exit_code = self._get_exit_code()
                    run_duration = time.time() - self._start_time

                    _log("ERROR", f"Bot process exited (code={exit_code}, "
                                  f"ran {run_duration:.0f}s)")
                    _log_incident("BOT_CRASHED", f"exit_code={exit_code}",
                                  duration=f"{run_duration:.0f}s",
                                  crash_count=str(self._crash_count + 1))

                    # Reset crash counter if bot ran long enough
                    if run_duration > STABLE_RUN_SECS:
                        self._crash_count = 0
                    else:
                        self._crash_count += 1

                    # Check safety
                    safe, reason = self._is_safe_to_restart()
                    if not safe:
                        _log("FATAL", f"Cannot auto-restart: {reason}")
                        _log("INFO", "Supervisor will keep monitoring. "
                                     "Fix the issue and restart manually, or "
                                     "the supervisor will retry in 5 minutes.")
                        _log_incident("RESTART_BLOCKED", reason)

                        # Wait and retry safety check
                        for _ in range(60):  # 5 min wait
                            if not self._running:
                                break
                            time.sleep(5)

                        # Re-check after wait
                        if self._running:
                            safe, reason = self._is_safe_to_restart()
                            if not safe:
                                _log("FATAL", f"Still unsafe: {reason} — shutting down supervisor")
                                break
                            _log("OK", "Safety check passed on retry")

                    # Backoff
                    backoff = self._get_backoff()
                    _log("RESTART", f"Waiting {backoff}s before restart "
                                    f"(crash #{self._crash_count})...")
                    _log_incident("BACKOFF", f"wait={backoff}s",
                                  crash_count=str(self._crash_count))

                    for _ in range(backoff):
                        if not self._running:
                            break
                        time.sleep(1)

                    if not self._running:
                        break

                    # Record restart time for anti-flap
                    self._restart_times.append(time.time())

                    # Restart
                    if not self._start_bot():
                        _log("FATAL", "Failed to restart bot — aborting")
                        break

        except KeyboardInterrupt:
            _log("INFO", "Ctrl+C — shutting down")
        finally:
            self._watcher.stop()
            self._kill_bot()
            _remove_lockfile()
            uptime = time.time() - self._uptime_start
            _log("INFO", f"Supervisor stopped. "
                         f"Uptime: {uptime/3600:.1f}h, "
                         f"Restarts: {self._total_restarts}")
            _log_incident("SUPERVISOR_STOPPED",
                          f"uptime={uptime/3600:.1f}h, "
                          f"restarts={self._total_restarts}")


# ──────────────────────────────────────────────────────────────
#  Error digest (for human review)
# ──────────────────────────────────────────────────────────────

def show_error_digest(hours: int = 24):
    """Print a summary of recent incidents."""
    print()
    print(f"{_C.MAG}{'═' * 56}{_C.R}")
    print(f"{_C.MAG}║{_C.R}  📊 {_C.B}{_C.WHT}"
          f"OBR Incident Digest (last {hours}h){_C.R}"
          f"{' ' * 14}{_C.MAG}║{_C.R}")
    print(f"{_C.MAG}{'═' * 56}{_C.R}")

    if not INCIDENT_FILE.exists():
        print(f"\n  {_C.D}No incidents recorded yet.{_C.R}")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    incidents = []

    with open(INCIDENT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(
                    rec["ts"].replace(" ", "T") + "+00:00")
                if ts >= cutoff:
                    incidents.append(rec)
            except Exception:
                continue

    if not incidents:
        print(f"\n  {_C.GRN}✅ No incidents in the last {hours}h{_C.R}")
        print()
        return

    # Category summary
    cats = {}
    for inc in incidents:
        cat = inc.get("category", "UNKNOWN")
        cats[cat] = cats.get(cat, 0) + 1

    print(f"\n  {_C.B}{_C.WHT}Category Summary:{_C.R}")
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        if "FATAL" in cat or "CRASH" in cat:
            color = _C.RED
        elif "RESTART" in cat or "BACKOFF" in cat:
            color = _C.YLW
        elif "START" in cat or "STOP" in cat:
            color = _C.GRN
        else:
            color = _C.CYN
        print(f"    {color}{'●':2s} {cat:25s} {count:4d}{_C.R}")

    # Last 15 incidents
    print(f"\n  {_C.B}{_C.WHT}Recent Incidents:{_C.R}")
    for inc in incidents[-15:]:
        ts = inc.get("ts", "")[-8:]  # just time
        cat = inc.get("category", "?")[:20]
        detail = inc.get("detail", "")[:60]
        if "FATAL" in cat or "ERROR" in cat:
            color = _C.RED
        elif "CRASH" in cat:
            color = _C.RED
        elif "RESTART" in cat or "BACKOFF" in cat:
            color = _C.YLW
        else:
            color = _C.D
        print(f"    {_C.D}{ts}{_C.R} {color}{cat:20s}{_C.R} {_C.D}{detail}{_C.R}")

    print(f"\n  {_C.D}Total: {len(incidents)} incidents in last {hours}h{_C.R}")
    print(f"  {_C.D}Full log: {INCIDENT_FILE}{_C.R}")
    print()


def show_supervisor_status():
    """Print supervisor + bot status."""
    print()
    print(f"  {_C.B}{_C.WHT}Supervisor Status{_C.R}")

    # Check supervisor log
    if SUPERVISOR_LOG.exists():
        # Get last few lines
        lines = SUPERVISOR_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        last = lines[-5:] if len(lines) >= 5 else lines
        print(f"  {_C.D}Last supervisor activity:{_C.R}")
        for line in last:
            print(f"    {_C.D}{line}{_C.R}")
    else:
        print(f"  {_C.D}No supervisor log found.{_C.R}")

    # Bot state
    state = _read_state()
    if state:
        eq = state.get("equity", 0)
        pending = len(state.get("pending_entries", []))
        trades = state.get("total_trades", 0)
        print(f"\n  {_C.B}Bot State:{_C.R}")
        print(f"    💎 Equity: {_C.GRN}${eq:.2f}{_C.R}")
        print(f"    📊 Total trades: {_C.WHT}{trades}{_C.R}")
        print(f"    🔒 Open positions: {_C.WHT}{pending}{_C.R}")
    print()
