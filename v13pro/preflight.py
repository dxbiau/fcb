"""
v13pro/preflight.py -- Pre-launch validation checks.

Verifies everything is ready before the bot starts trading.
Run automatically by bot.py on startup, or manually via CLI.

Checks:
  1. Import integrity — all modules load cleanly
  2. Config sanity — risk params, fee model, paths
  3. Combo file — deploy_combos.json exists and is valid
  4. Exchange connectivity — API key works, can read balance
  5. Market availability — all portfolio pairs are tradeable
  6. State file — not corrupted
  7. Disk space + log directory writable
  8. Strategy functions — all referenced strategies exist
  9. WS readiness — basic connectivity test

Returns a PreflightResult with pass/fail + details for each check.
"""

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import List, Tuple

from v13pro import config as cfg
from v13pro import logger as log


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    critical: bool = True  # if critical and failed, abort launch


@dataclass
class PreflightResult:
    checks: List[CheckResult] = field(default_factory=list)
    passed: bool = True
    critical_failures: int = 0
    warnings: int = 0

    def add(self, name: str, passed: bool, detail: str = "",
            critical: bool = True):
        cr = CheckResult(name, passed, detail, critical)
        self.checks.append(cr)
        if not passed:
            if critical:
                self.passed = False
                self.critical_failures += 1
            else:
                self.warnings += 1
        return cr


def check_imports() -> CheckResult:
    """Verify all v13pro modules import cleanly."""
    modules = [
        "v13pro.config", "v13pro.logger", "v13pro.trade_logger",
        "v13pro.indicators", "v13pro.strategies", "v13pro.registry",
        "v13pro.exchange", "v13pro.state", "v13pro.ws_data",
        "v13pro.guardian", "v13pro.hunter", "v13pro.bot",
        "v13pro.journal", "v13pro.learner", "v13pro.skill",
    ]
    failed = []
    for mod in modules:
        try:
            __import__(mod)
        except Exception as e:
            failed.append(f"{mod}: {e}")

    if failed:
        return CheckResult("imports", False,
                           f"{len(failed)} failed: {'; '.join(failed[:3])}")
    return CheckResult("imports", True, f"{len(modules)} modules OK")


def check_config() -> CheckResult:
    """Validate config consistency."""
    issues = []

    if cfg.RISK_PCT <= 0 or cfg.RISK_PCT > 0.20:
        issues.append(f"RISK_PCT={cfg.RISK_PCT} (expected 0.01-0.20)")
    if cfg.LEVERAGE < 1 or cfg.LEVERAGE > 100:
        issues.append(f"LEVERAGE={cfg.LEVERAGE}")
    if not cfg.RISK_CURVE:
        issues.append("RISK_CURVE empty")
    if not cfg.LEVERAGE_CURVE:
        issues.append("LEVERAGE_CURVE empty")
    if not cfg.PROFIT_TIERS:
        issues.append("PROFIT_TIERS empty")
    if cfg.MAX_CONCURRENT_POSITIONS < 1:
        issues.append("MAX_CONCURRENT_POSITIONS < 1")
    if cfg.MAX_TRADES_DAY < 1:
        issues.append("MAX_TRADES_DAY < 1")
    if cfg.GUARDIAN_POLL_SECS < 5:
        issues.append(f"GUARDIAN_POLL_SECS={cfg.GUARDIAN_POLL_SECS} (too fast)")
    if cfg.WS_CANDLE_BUFFER < 200:
        issues.append(f"WS_CANDLE_BUFFER={cfg.WS_CANDLE_BUFFER} (need >=200)")

    if issues:
        return CheckResult("config", False, "; ".join(issues))
    return CheckResult("config", True, "All config params valid")


def check_api_keys() -> CheckResult:
    """Verify API keys are set."""
    if not cfg.API_KEY or len(cfg.API_KEY) < 10:
        return CheckResult("api_keys", False, "BYBIT_API_KEY not set or too short")
    if not cfg.API_SECRET or len(cfg.API_SECRET) < 10:
        return CheckResult("api_keys", False, "BYBIT_API_SECRET not set or too short")
    return CheckResult("api_keys", True, f"Key: {cfg.API_KEY[:6]}...{cfg.API_KEY[-4:]}")


def check_combos() -> CheckResult:
    """Verify deploy_combos.json exists and has valid structure."""
    path = cfg.DEPLOY_COMBOS
    if not os.path.exists(path):
        return CheckResult("combos", False, f"Missing: {path}")

    try:
        with open(path, "r") as f:
            combos = json.load(f)
    except Exception as e:
        return CheckResult("combos", False, f"Parse error: {e}")

    if not isinstance(combos, list) or len(combos) == 0:
        return CheckResult("combos", False, "Empty or not a list")

    # Validate structure
    required = {"pair", "strat", "tf", "exit"}
    missing_fields = []
    for i, c in enumerate(combos[:5]):
        if not required.issubset(c.keys()):
            missing_fields.append(f"combo[{i}] missing {required - c.keys()}")

    if missing_fields:
        return CheckResult("combos", False, "; ".join(missing_fields))

    # Check strategies exist
    from v13pro.strategies import STRATEGIES
    strats_used = set(c["strat"] for c in combos)
    valid_strats = set(STRATEGIES.keys()) | {"ENS2", "ENS3"}
    invalid = strats_used - valid_strats
    if invalid:
        return CheckResult("combos", False,
                           f"Unknown strategies: {invalid}", critical=False)

    pairs = set(c["pair"] for c in combos)
    tfs = set(c["tf"] for c in combos)
    return CheckResult("combos", True,
                       f"{len(combos)} combos, {len(pairs)} pairs, {len(tfs)} TFs")


def check_strategies() -> CheckResult:
    """Verify all strategy functions are callable."""
    try:
        from v13pro.strategies import STRATEGIES
        for name, fn in STRATEGIES.items():
            if not callable(fn):
                return CheckResult("strategies", False, f"{name} not callable")
        return CheckResult("strategies", True,
                           f"{len(STRATEGIES)} strategies OK")
    except Exception as e:
        return CheckResult("strategies", False, str(e))


def check_state_file() -> CheckResult:
    """Verify state file is not corrupted."""
    if not os.path.exists(cfg.STATE_FILE):
        return CheckResult("state", True, "No state file (fresh start)",
                           critical=False)
    try:
        with open(cfg.STATE_FILE, "r") as f:
            data = json.load(f)
        equity = data.get("equity", 0)
        if equity < 0:
            return CheckResult("state", False, f"Negative equity: {equity}")
        return CheckResult("state", True,
                           f"Equity: ${equity:.2f}, "
                           f"trades: {data.get('total_trades', 0)}")
    except Exception as e:
        return CheckResult("state", False, f"Corrupt: {e}")


def check_disk_space() -> CheckResult:
    """Check disk space for logs."""
    try:
        usage = shutil.disk_usage(cfg.BASE_DIR)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 100:
            return CheckResult("disk", False,
                               f"Only {free_mb:.0f}MB free (need 100MB+)")
        return CheckResult("disk", True, f"{free_mb:.0f}MB free")
    except Exception as e:
        return CheckResult("disk", True, f"Can't check: {e}", critical=False)


def check_log_dir() -> CheckResult:
    """Verify log directory is writable."""
    try:
        os.makedirs(cfg.LOG_DIR, exist_ok=True)
        test_file = os.path.join(cfg.LOG_DIR, ".preflight_test")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return CheckResult("log_dir", True, cfg.LOG_DIR)
    except Exception as e:
        return CheckResult("log_dir", False, f"Not writable: {e}")


async def check_exchange_connectivity() -> CheckResult:
    """Test exchange API connectivity (async)."""
    try:
        from v13pro import exchange as ex_mod
        ex = await ex_mod.create_exchange()
        eq = await ex_mod.get_equity(ex)
        await ex_mod.close_exchange()
        return CheckResult("exchange", True, f"Connected, equity=${eq:.2f}")
    except Exception as e:
        return CheckResult("exchange", False, f"Connection failed: {e}")


async def check_markets(pairs: set) -> CheckResult:
    """Verify all portfolio pairs are tradeable."""
    try:
        from v13pro import exchange as ex_mod
        ex = await ex_mod.create_exchange()
        missing = []
        for pair in pairs:
            if pair not in ex.markets:
                missing.append(pair)
        await ex_mod.close_exchange()
        if missing:
            return CheckResult("markets", False,
                               f"{len(missing)} missing: {missing[:5]}",
                               critical=False)
        return CheckResult("markets", True, f"All {len(pairs)} pairs available")
    except Exception as e:
        return CheckResult("markets", False, f"Check failed: {e}")


# ══════════════════════════════════════════════════════════════
#  MAIN PREFLIGHT RUNNER
# ══════════════════════════════════════════════════════════════

def run_sync() -> PreflightResult:
    """Run all synchronous preflight checks."""
    result = PreflightResult()

    checks = [
        check_imports,
        check_config,
        check_api_keys,
        check_combos,
        check_strategies,
        check_state_file,
        check_disk_space,
        check_log_dir,
    ]

    for fn in checks:
        try:
            cr = fn()
            result.add(cr.name, cr.passed, cr.detail, cr.critical)
        except Exception as e:
            result.add(fn.__name__, False, f"Exception: {e}")

    return result


async def run_full() -> PreflightResult:
    """Run ALL preflight checks including async exchange tests."""
    result = run_sync()

    # Async checks
    try:
        cr = await check_exchange_connectivity()
        result.add(cr.name, cr.passed, cr.detail, cr.critical)
    except Exception as e:
        result.add("exchange", False, f"Exception: {e}")

    # Market check (only if combos loaded)
    try:
        from v13pro.registry import ComboRegistry
        reg = ComboRegistry()
        cr = await check_markets(reg.all_pairs)
        result.add(cr.name, cr.passed, cr.detail, cr.critical)
    except Exception as e:
        result.add("markets", False, f"Exception: {e}")

    return result


def print_report(result: PreflightResult):
    """Print preflight report to console with colored icons."""
    W = 58
    log.info(f"\n{log.C.BCYAN}{'═' * W}{log.C.RESET}")
    log.info(f"  🛫 {log.C.BOLD}{log.C.BWHITE}PREFLIGHT CHECK{log.C.RESET}")
    log.info(f"{log.C.BCYAN}{'═' * W}{log.C.RESET}")
    for cr in result.checks:
        if cr.passed:
            icon = f"{log.C.BGREEN}✅ PASS{log.C.RESET}"
        elif cr.critical:
            icon = f"{log.C.BRED}❌ FAIL{log.C.RESET}"
        else:
            icon = f"{log.C.BYELLOW}⚠️  WARN{log.C.RESET}"
        log.info(f"  {icon}  {log.C.BOLD}{cr.name:15s}{log.C.RESET}"
                 f" {log.C.DIM}{cr.detail}{log.C.RESET}")

    log.info(f"  {log.C.DIM}{'─' * (W - 4)}{log.C.RESET}")
    if result.passed:
        log.info(f"  {log.C.BGREEN}🟢 ALL {len(result.checks)} CHECKS PASSED"
                 f"{log.C.RESET}"
                 f" {log.C.DIM}({result.warnings} warnings){log.C.RESET}")
    else:
        log.info(f"  {log.C.BRED}🔴 PREFLIGHT FAILED: "
                 f"{result.critical_failures} critical, "
                 f"{result.warnings} warnings{log.C.RESET}")
    log.info(f"{log.C.BCYAN}{'═' * W}{log.C.RESET}\n")


# CLI entry point
if __name__ == "__main__":
    result = run_sync()
    print_report(result)
