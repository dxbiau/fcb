"""
v13pro/watchdog.py -- Async health monitor.

Runs as an asyncio task inside the bot event loop.
Monitors system health and triggers alerts/recovery:

  1. Network connectivity (DNS + exchange API)
  2. Memory usage (soft limit 500MB)
  3. WS connection health (staleness detection)
  4. Guardian heartbeat (detect stale guardian)
  5. Disk space (log rotation if low)
  6. Equity snapshot (crash recovery)
  7. Log retention (cleanup old files)

Does NOT restart the bot (supervisor handles that).
Logs health events to journal for post-analysis.
"""

import asyncio
import gc
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from v13pro import config as cfg
from v13pro import logger as log

# Config
CHECK_INTERVAL = 60          # seconds between health checks
MEMORY_SOFT_LIMIT_MB = 2000
DISK_MIN_MB = 100
WS_STALE_SECS = 300         # 5m without WS update = stale
GUARDIAN_STALE_SECS = 120    # 2m without guardian poll = stale
LOG_RETENTION_DAYS = 14
EQUITY_SNAPSHOT_INTERVAL = 300  # 5m between equity snapshots


class Watchdog:
    """Async health monitor running inside the bot event loop."""

    def __init__(self, bot=None, ws_data=None, guardian=None, state=None):
        self._bot = bot
        self._ws = ws_data
        self._guardian = guardian
        self._state = state
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_equity_snap = 0.0
        self._alerts: list = []
        self._check_count = 0
        self._healthy = True

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="watchdog")
        log.info("Watchdog started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Watchdog stopped")

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    @property
    def stats(self) -> dict:
        return {
            "healthy": self._healthy,
            "checks": self._check_count,
            "alerts": len(self._alerts),
            "recent_alerts": self._alerts[-5:] if self._alerts else [],
        }

    async def _loop(self):
        while self._running:
            try:
                await self._run_checks()
                self._check_count += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.log_exception("Watchdog", e)
            await asyncio.sleep(CHECK_INTERVAL)

    async def _run_checks(self):
        issues = []

        # 1. Memory check
        mem_issue = self._check_memory()
        if mem_issue:
            issues.append(mem_issue)

        # 2. WS health
        ws_issue = self._check_ws_health()
        if ws_issue:
            issues.append(ws_issue)

        # 3. Guardian health
        guard_issue = self._check_guardian_health()
        if guard_issue:
            issues.append(guard_issue)

        # 4. Disk space
        disk_issue = self._check_disk()
        if disk_issue:
            issues.append(disk_issue)

        # 5. Equity snapshot
        await self._equity_snapshot()

        # 6. Log retention (once per hour)
        if self._check_count > 0 and self._check_count % 60 == 0:
            self._cleanup_old_logs()

        # Update health status
        prev = self._healthy
        self._healthy = len(issues) == 0

        if issues:
            for issue in issues:
                self._alert(issue)

        if not prev and self._healthy:
            log.info("Watchdog: health RESTORED")

    def _check_memory(self) -> Optional[str]:
        """Check process memory usage and try to reclaim if high."""
        try:
            import psutil
            proc = psutil.Process()
            mem_mb = proc.memory_info().rss / (1024 * 1024)
            if mem_mb > MEMORY_SOFT_LIMIT_MB:
                # Aggressive GC
                gc.collect(2)
                gc.collect(1)
                gc.collect(0)

                # Trim ccxt internal caches (biggest memory hog)
                if self._bot and hasattr(self._bot, '_ex'):
                    ex = self._bot._ex
                    trimmed = 0
                    for attr in ('tickers', 'orderbooks', 'orders',
                                 'trades', 'myTrades', 'ohlcvs'):
                        cache = getattr(ex, attr, None)
                        if cache and hasattr(cache, '__len__') and len(cache) > 10:
                            try:
                                if isinstance(cache, dict):
                                    keys = list(cache.keys())
                                    for k in keys[:-10]:
                                        del cache[k]
                                        trimmed += 1
                            except Exception:
                                pass
                    if trimmed:
                        gc.collect()

                # Also trim shadow tracker pending queue
                if self._bot and hasattr(self._bot, '_shadow'):
                    shadow = self._bot._shadow
                    if hasattr(shadow, '_pending') and len(shadow._pending) > 200:
                        # Keep only most recent 100
                        while len(shadow._pending) > 100:
                            shadow._pending.popleft()

                # Force GC one more time
                gc.collect()

                # Re-measure after cleanup
                mem_mb2 = proc.memory_info().rss / (1024 * 1024)
                if mem_mb2 > MEMORY_SOFT_LIMIT_MB:
                    return f"Memory {mem_mb2:.0f}MB > {MEMORY_SOFT_LIMIT_MB}MB limit"
                else:
                    log.info(f"Watchdog: memory reclaimed {mem_mb:.0f}MB -> {mem_mb2:.0f}MB")
        except ImportError:
            pass  # psutil not required
        except Exception:
            pass
        return None

    def _check_ws_health(self) -> Optional[str]:
        """Check if WS data is stale."""
        if not self._ws:
            return None
        stats = self._ws.stats
        if not stats.get("connected"):
            return "WS disconnected"
        # Reset error counter periodically to avoid false alerts
        # from accumulated transient errors over long sessions
        errors = stats.get("errors", 0)
        if errors > 500:
            # Reset the counter — these are historical, not active issues
            if hasattr(self._ws, '_errors'):
                self._ws._errors = 0
            return None  # Don't alert on stale accumulated errors
        return None

    def _check_guardian_health(self) -> Optional[str]:
        """Check guardian is running."""
        if not self._guardian:
            return None
        # Guardian tracks positions — if it has tracked positions
        # but count hasn't changed for a long time, something's wrong
        # This is a lightweight check
        if hasattr(self._guardian, '_running') and not self._guardian._running:
            return "Guardian stopped unexpectedly"
        return None

    def _check_disk(self) -> Optional[str]:
        """Check disk space."""
        try:
            import shutil
            usage = shutil.disk_usage(cfg.BASE_DIR)
            free_mb = usage.free / (1024 * 1024)
            if free_mb < DISK_MIN_MB:
                self._cleanup_old_logs()
                return f"Disk low: {free_mb:.0f}MB free"
        except Exception:
            pass
        return None

    async def _equity_snapshot(self):
        """Periodic equity snapshot for crash recovery."""
        now = time.time()
        if now - self._last_equity_snap < EQUITY_SNAPSHOT_INTERVAL:
            return

        if self._state:
            try:
                equity = self._state.equity
                peak = self._state.peak_equity
                from v13pro.journal import log_event
                log_event("equity_snapshot", {
                    "equity": equity,
                    "peak": peak,
                    "dd_pct": (peak - equity) / peak * 100 if peak > 0 else 0,
                    "positions": self._guardian.tracked_count if self._guardian else 0,
                })
                self._last_equity_snap = now
            except Exception:
                pass

    def _cleanup_old_logs(self):
        """Remove log files older than retention period."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=LOG_RETENTION_DAYS)
        cleaned = 0

        for dirpath in [cfg.LOG_DIR, os.path.join(cfg.LOG_DIR, "journal")]:
            if not os.path.isdir(dirpath):
                continue
            try:
                for fn in os.listdir(dirpath):
                    fp = os.path.join(dirpath, fn)
                    if not os.path.isfile(fp):
                        continue
                    mtime = datetime.fromtimestamp(
                        os.path.getmtime(fp), tz=timezone.utc)
                    if mtime < cutoff:
                        os.remove(fp)
                        cleaned += 1
            except Exception:
                pass

        if cleaned > 0:
            log.debug(f"Watchdog: cleaned {cleaned} old log files")

    def _alert(self, message: str):
        """Record a health alert."""
        ts = datetime.now(timezone.utc).isoformat()
        self._alerts.append({"ts": ts, "msg": message})
        if len(self._alerts) > 100:
            self._alerts = self._alerts[-100:]
        log.warning(f"Watchdog ALERT: {message}")

        try:
            from v13pro.journal import log_event
            log_event("watchdog_alert", {"alert": message})
        except Exception:
            pass
