"""
watchdog.py — 24/7 Production Supervisor for FCB Bot

Wraps run_live.py with self-diagnostics to maximise uptime:

  1. AUTO-RESTART   — Restarts the bot on crash with exponential backoff
  2. CLOCK SYNC     — Detects and corrects clock drift (caused STBL trail failure)
  3. NETWORK CHECK  — Verifies DNS + Bybit API reachability before restart
  4. MEMORY MONITOR — Detects memory leaks, restarts if threshold exceeded
  5. GUARDIAN HEALTH — Monitors profit_guardian thread liveness via heartbeat file
  6. DISK SPACE     — Checks free space, rotates old logs
  7. GRACEFUL STOP  — Handles SIGTERM/SIGINT for clean container shutdown

Usage:
  python watchdog.py          # Production (replaces run_live.py as entrypoint)
  python watchdog.py --dry    # Diagnostics only, don't start bot

Design:
  The watchdog runs as PID 1 in the container. It spawns run_live.py as a
  subprocess, monitors it, and restarts on failure. The bot itself handles
  all trading logic — the watchdog only handles ops.

Backoff schedule (consecutive crashes):
  1st crash  → wait 10s
  2nd crash  → wait 30s
  3rd crash  → wait 60s
  4th crash  → wait 120s
  5th+ crash → wait 300s (5 min cap)
  Successful run (>5 min) resets the counter.
"""

import os
import sys
import time
import signal
import subprocess
import shutil
import json
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ─── Configuration ───────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
BOT_CMD = [sys.executable, str(BASE_DIR / "run_live.py"), "--yes"]
HEARTBEAT_FILE = BASE_DIR / "live" / "logs" / "watchdog_heartbeat.json"
LOG_DIR = BASE_DIR / "live" / "logs"
STATE_FILE = BASE_DIR / "live" / "state.json"

# Restart backoff schedule (seconds)
BACKOFF_SCHEDULE = [10, 30, 60, 120, 300]
# If bot runs longer than this, reset crash counter
STABLE_RUN_SECS = 300  # 5 minutes

# Memory limit (MB) — restart if bot exceeds this
MEMORY_LIMIT_MB = 400

# Clock drift threshold (seconds) — sync if drift exceeds this
CLOCK_DRIFT_THRESHOLD_SECS = 2.0

# Disk space minimum (MB) — warn if below
DISK_SPACE_MIN_MB = 100

# Log retention — delete logs older than this
LOG_RETENTION_DAYS = 14

# Health check interval (seconds)
HEALTH_CHECK_INTERVAL = 60

# Guardian heartbeat staleness threshold (seconds)
GUARDIAN_HEARTBEAT_STALE_SECS = 120  # Guardian polls every 2s, 120s means 60 missed polls


# ─── Logging ─────────────────────────────────────────────────

def log(level: str, msg: str):
    """Simple timestamped log to stdout (Docker captures this)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} │ {level:<7} │ [WATCHDOG] {msg}", flush=True)


def log_info(msg): log("INFO", msg)
def log_warn(msg): log("WARNING", msg)
def log_error(msg): log("ERROR", msg)
def log_crit(msg): log("CRIT", msg)


# ─── Diagnostics ─────────────────────────────────────────────

def check_network() -> bool:
    """Verify DNS resolution and Bybit API reachability."""
    import urllib.request
    import urllib.error

    # 1. DNS check
    try:
        import socket
        socket.setdefaulttimeout(10)
        socket.getaddrinfo("api.bybit.com", 443)
    except Exception as e:
        log_error(f"DNS resolution failed: {e}")
        return False

    # 2. Bybit API check
    try:
        req = urllib.request.Request(
            "https://api.bybit.com/v5/market/time",
            headers={"User-Agent": "FCB-Watchdog/1.0"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if data.get("retCode") == 0:
            log_info("Network: Bybit API reachable ✓")
            return True
        else:
            log_error(f"Bybit API error: {data}")
            return False
    except Exception as e:
        log_error(f"Bybit API unreachable: {e}")
        return False


def check_clock_drift() -> float:
    """Check clock drift against Bybit's server.
    Returns drift in seconds (positive = local ahead).
    """
    import urllib.request

    try:
        t1 = time.time()
        req = urllib.request.Request(
            "https://api.bybit.com/v5/market/time",
            headers={"User-Agent": "FCB-Watchdog/1.0"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        t2 = time.time()
        data = json.loads(resp.read())
        server_ns = int(data["result"]["timeNano"])
        server_s = server_ns / 1e9
        # Estimate: local time at midpoint of request
        local_s = (t1 + t2) / 2
        drift = local_s - server_s
        return drift
    except Exception as e:
        log_warn(f"Clock drift check failed: {e}")
        return 0.0


def sync_clock():
    """Attempt to sync system clock (requires ntpdate on Linux)."""
    try:
        result = subprocess.run(
            ["ntpdate", "-s", "pool.ntp.org"],
            capture_output=True, timeout=15
        )
        if result.returncode == 0:
            log_info("Clock synced via NTP ✓")
        else:
            log_warn(f"NTP sync returned code {result.returncode}")
    except FileNotFoundError:
        log_warn("ntpdate not found — clock sync skipped")
    except Exception as e:
        log_warn(f"Clock sync failed: {e}")


def check_memory(pid: int) -> float:
    """Get memory usage of bot process in MB."""
    try:
        # Linux: read from /proc
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return kb / 1024.0
    except FileNotFoundError:
        pass

    # Fallback: psutil-like via /proc/statm
    try:
        with open(f"/proc/{pid}/statm") as f:
            pages = int(f.read().split()[1])  # resident pages
            page_size = os.sysconf("SC_PAGE_SIZE")
            return (pages * page_size) / (1024 * 1024)
    except Exception:
        pass

    return 0.0  # Can't measure


def check_disk_space() -> float:
    """Get free disk space in MB for the log directory."""
    try:
        usage = shutil.disk_usage(str(LOG_DIR))
        return usage.free / (1024 * 1024)
    except Exception:
        return float("inf")  # Can't check, assume OK


def rotate_old_logs():
    """Delete log files older than LOG_RETENTION_DAYS."""
    if not LOG_DIR.exists():
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOG_RETENTION_DAYS)
    removed = 0
    for f in LOG_DIR.glob("*.log"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                removed += 1
        except Exception:
            pass

    if removed:
        log_info(f"Log rotation: removed {removed} files older than {LOG_RETENTION_DAYS} days")


def check_guardian_heartbeat() -> bool:
    """Check if the Profit Guardian thread is alive via heartbeat file.

    The guardian writes a heartbeat timestamp every poll cycle.
    If it's stale, the thread has likely died silently.
    """
    if not HEARTBEAT_FILE.exists():
        return True  # No heartbeat file yet — bot hasn't started a position

    try:
        with open(HEARTBEAT_FILE) as f:
            data = json.load(f)
        last_beat = datetime.fromisoformat(data.get("last_beat", ""))
        age = (datetime.now(timezone.utc) - last_beat).total_seconds()

        if age > GUARDIAN_HEARTBEAT_STALE_SECS:
            log_error(
                f"Guardian heartbeat STALE — last beat {age:.0f}s ago "
                f"(threshold: {GUARDIAN_HEARTBEAT_STALE_SECS}s)"
            )
            return False
        return True
    except Exception as e:
        log_warn(f"Guardian heartbeat check error: {e}")
        return True  # Don't kill bot over a read error


def check_api_keys() -> bool:
    """Verify API keys are set in environment."""
    key = os.environ.get("BYBIT_API_KEY", "")
    secret = os.environ.get("BYBIT_API_SECRET", "")
    if not key or not secret:
        log_crit("BYBIT_API_KEY / BYBIT_API_SECRET not set in environment!")
        return False
    if key == "your_mainnet_api_key_here":
        log_crit("API keys are placeholder values — edit .env file!")
        return False
    log_info(f"API keys: set ✓ (key ends ...{key[-4:]})")
    return True


def check_state_file() -> bool:
    """Verify state.json is readable and not corrupted."""
    if not STATE_FILE.exists():
        log_info("State file: not found (will be created on start)")
        return True

    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        log_info(f"State file: OK — {data.get('total_trades', 0)} trades, "
                 f"equity=${data.get('equity', 0):.2f}")
        return True
    except json.JSONDecodeError as e:
        log_error(f"State file CORRUPTED: {e}")
        # Back up the corrupted file
        backup = STATE_FILE.with_suffix(f".corrupted_{int(time.time())}.json")
        shutil.copy2(STATE_FILE, backup)
        log_warn(f"Corrupted state backed up to {backup.name}")
        STATE_FILE.unlink()
        log_info("Corrupted state file removed — bot will create fresh state")
        return True
    except Exception as e:
        log_error(f"State file error: {e}")
        return True  # Non-fatal — bot can recreate


def run_all_diagnostics() -> bool:
    """Run all pre-start diagnostics. Returns True if OK to start."""
    log_info("=" * 60)
    log_info("  WATCHDOG DIAGNOSTICS")
    log_info("=" * 60)

    passed = True

    # 1. API Keys
    if not check_api_keys():
        return False  # Fatal — can't trade without keys

    # 2. Network
    if not check_network():
        log_warn("Network check failed — will retry with backoff")
        passed = False

    # 3. Clock drift
    drift = check_clock_drift()
    abs_drift = abs(drift)
    if abs_drift > CLOCK_DRIFT_THRESHOLD_SECS:
        log_warn(f"Clock drift: {drift:+.3f}s (threshold: {CLOCK_DRIFT_THRESHOLD_SECS}s)")
        sync_clock()
        # Re-check after sync
        drift = check_clock_drift()
        if abs(drift) > CLOCK_DRIFT_THRESHOLD_SECS:
            log_warn(f"Clock still drifted after sync: {drift:+.3f}s — "
                     f"recvWindow=20s should handle this")
    else:
        log_info(f"Clock drift: {drift:+.3f}s ✓")

    # 4. Disk space
    free_mb = check_disk_space()
    if free_mb < DISK_SPACE_MIN_MB:
        log_warn(f"Low disk space: {free_mb:.0f}MB free (min: {DISK_SPACE_MIN_MB}MB)")
        rotate_old_logs()
    else:
        log_info(f"Disk space: {free_mb:.0f}MB free ✓")

    # 5. Log rotation
    rotate_old_logs()

    # 6. State file
    check_state_file()

    # 7. Lock file cleanup (stale from previous crash)
    lock_file = BASE_DIR / "live" / "bot.lock"
    if lock_file.exists():
        log_info("Removing stale lock file from previous run")
        lock_file.unlink(missing_ok=True)

    log_info("=" * 60)
    if passed:
        log_info("  DIAGNOSTICS PASSED — ready to start")
    else:
        log_warn("  DIAGNOSTICS WARNING — starting with issues")
    log_info("=" * 60)

    return passed


# ─── Supervisor Loop ─────────────────────────────────────────

class Watchdog:
    """Supervises the bot process with auto-restart and health monitoring."""

    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.crash_count = 0
        self.start_time = 0.0
        self._running = True
        self._setup_signals()

    def _setup_signals(self):
        """Handle SIGTERM/SIGINT gracefully (Docker sends SIGTERM on stop)."""
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        """Graceful shutdown — forward signal to bot, wait, then exit."""
        sig_name = signal.Signals(signum).name
        log_info(f"Received {sig_name} — initiating graceful shutdown")
        self._running = False

        if self.process and self.process.poll() is None:
            log_info("Sending SIGTERM to bot process...")
            self.process.terminate()
            try:
                self.process.wait(timeout=25)  # Docker gives 30s grace
                log_info("Bot stopped gracefully ✓")
            except subprocess.TimeoutExpired:
                log_warn("Bot didn't stop in time — sending SIGKILL")
                self.process.kill()
                self.process.wait(timeout=5)

    def _get_backoff(self) -> int:
        """Get current backoff delay based on crash count."""
        idx = min(self.crash_count, len(BACKOFF_SCHEDULE) - 1)
        return BACKOFF_SCHEDULE[idx]

    def _start_bot(self):
        """Start the bot subprocess."""
        log_info(f"Starting bot: {' '.join(BOT_CMD)}")
        self.start_time = time.time()

        # Ensure log directory exists
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        self.process = subprocess.Popen(
            BOT_CMD,
            stdout=sys.stdout,    # Inherit stdout (Docker logs)
            stderr=sys.stderr,    # Inherit stderr
            cwd=str(BASE_DIR),
        )
        log_info(f"Bot started — PID {self.process.pid}")

    def _health_check(self):
        """Run periodic health checks while bot is running."""
        if not self.process or self.process.poll() is not None:
            return  # Bot not running

        pid = self.process.pid

        # Memory check
        mem_mb = check_memory(pid)
        if mem_mb > 0:
            if mem_mb > MEMORY_LIMIT_MB:
                log_crit(
                    f"Memory limit exceeded: {mem_mb:.0f}MB > {MEMORY_LIMIT_MB}MB — "
                    f"restarting bot"
                )
                self.process.terminate()
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                return

        # Clock drift check (just log, don't kill)
        drift = check_clock_drift()
        if abs(drift) > CLOCK_DRIFT_THRESHOLD_SECS:
            log_warn(f"Clock drift detected: {drift:+.3f}s — attempting sync")
            sync_clock()

        # Guardian heartbeat check
        if not check_guardian_heartbeat():
            log_warn("Guardian thread may be dead — will restart bot if confirmed")
            time.sleep(30)  # Wait and double-check
            if not check_guardian_heartbeat():
                log_crit("Guardian thread confirmed dead — restarting bot")
                self.process.terminate()
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                return

        # Disk space (periodic)
        free_mb = check_disk_space()
        if free_mb < DISK_SPACE_MIN_MB:
            log_warn(f"Low disk: {free_mb:.0f}MB — rotating old logs")
            rotate_old_logs()

    def run(self):
        """Main supervisor loop."""
        log_info("=" * 60)
        log_info("  FCB WATCHDOG v1.0 — 24/7 Production Supervisor")
        log_info("=" * 60)

        # Pre-start diagnostics
        run_all_diagnostics()

        last_health_check = 0

        while self._running:
            # Start/restart bot
            if self.process is None or self.process.poll() is not None:
                if self.process is not None:
                    # Bot exited — check why
                    exit_code = self.process.returncode
                    run_duration = time.time() - self.start_time

                    if exit_code == 0:
                        log_info(f"Bot exited cleanly (code 0) after {run_duration:.0f}s")
                        if not self._running:
                            break  # Intentional shutdown
                        # Clean exit but we're still running — restart
                        self.crash_count = 0
                    else:
                        self.crash_count += 1
                        log_error(
                            f"Bot CRASHED — exit code {exit_code}, "
                            f"ran for {run_duration:.0f}s, "
                            f"crash #{self.crash_count}"
                        )

                        # If it ran long enough, reset crash counter
                        if run_duration > STABLE_RUN_SECS:
                            log_info(
                                f"Bot ran for {run_duration:.0f}s (> {STABLE_RUN_SECS}s) "
                                f"— resetting crash counter"
                            )
                            self.crash_count = 1  # Still count this crash

                    backoff = self._get_backoff()
                    log_info(f"Restarting in {backoff}s (backoff level {self.crash_count})")

                    # Wait with network check
                    deadline = time.time() + backoff
                    while time.time() < deadline and self._running:
                        time.sleep(1)

                    if not self._running:
                        break

                    # Pre-restart diagnostics
                    max_net_retries = 10
                    for attempt in range(max_net_retries):
                        if check_network():
                            break
                        log_warn(f"Network not ready — retry {attempt+1}/{max_net_retries}")
                        time.sleep(10)
                    else:
                        log_error("Network still down after retries — starting anyway, "
                                  "bot has its own retry logic")

                self._start_bot()
                last_health_check = time.time()

            # Health checks
            now = time.time()
            if now - last_health_check >= HEALTH_CHECK_INTERVAL:
                try:
                    self._health_check()
                except Exception as e:
                    log_warn(f"Health check error: {e}")
                last_health_check = now

            # Wait for process or next check
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # Still running — good

        log_info("Watchdog shutdown complete.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="FCB Bot — 24/7 Watchdog Supervisor")
    parser.add_argument("--dry", action="store_true",
                        help="Run diagnostics only, don't start the bot")
    args = parser.parse_args()

    if args.dry:
        run_all_diagnostics()
        sys.exit(0)

    watchdog = Watchdog()
    watchdog.run()


if __name__ == "__main__":
    main()
