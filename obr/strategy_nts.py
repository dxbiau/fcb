"""
obr/strategy_nts.py -- N_TREND_STOCH signal detection & trade computation.

Replaces OBR with the walk-forward validated strategy from Round 3-5.

Strategy: EMA Stacked Trend + Stochastic Pullback
  LONG:
    - EMA(8) > EMA(21) > EMA(50)  (confirmed uptrend)
    - Stoch(14) K crosses above D from below 30  (oversold pullback)
    - Bullish candle with body >= 40% of range
  SHORT:
    - EMA(8) < EMA(21) < EMA(50)  (confirmed downtrend)
    - Stoch(14) K crosses below D from above 70  (overbought pullback)
    - Bearish candle with body >= 40% of range

Timeframe: 1H (aggregated from 5m candles or direct 1H fetch)
Walk-forward validated: PF 2.09, WR 48%, robust across all parameters.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict
import numpy as np
from obr.config import (
    TP_R, FEE_RATE, RISK_PCT, LEVERAGE, MAX_CONCURRENT_POSITIONS,
    MIN_RISK_DISTANCE_PCT, EXCHANGE_TP_R,
)


# ==================================================================
#  DATA CLASSES (keep same interface for bot.py compatibility)
# ==================================================================

@dataclass
class CandleData:
    """Minimal candle representation."""
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_bearish(self) -> bool:
        return self.open > self.close

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass
class NTSSignal:
    """N_TREND_STOCH signal."""
    symbol: str
    direction: str           # "long" or "short"
    candle: CandleData       # the signal candle
    ema8: float
    ema21: float
    ema50: float
    stoch_k: float
    stoch_d: float
    atr14: float
    signal_type: int         # 2=long, 1=short

    @property
    def stop_loss_price(self) -> float:
        if self.direction == "long":
            return self.candle.low - 0.4 * self.atr14
        else:
            return self.candle.high + 0.4 * self.atr14


# Keep OBRSignal as alias for backward compatibility
OBRSignal = NTSSignal


@dataclass
class TradeSignal:
    """Computed trade ready for execution."""
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_per_unit: float
    dollar_risk: float
    position_size: float
    fee_r: float
    exchange_tp: float
    ob_candle: CandleData     # renamed for compat (actually the signal candle)
    prev_candle: CandleData   # previous candle for logging


# ==================================================================
#  INDICATOR COMPUTATION
# ==================================================================

class IndicatorState:
    """Maintains rolling indicator state for a single pair."""

    def __init__(self):
        self.closes: List[float] = []
        self.highs: List[float] = []
        self.lows: List[float] = []
        self.opens: List[float] = []
        self.volumes: List[float] = []

        # EMA states (initialized lazily)
        self._ema8: float = 0.0
        self._ema21: float = 0.0
        self._ema50: float = 0.0
        self._ema8_k: float = 2 / 9
        self._ema21_k: float = 2 / 22
        self._ema50_k: float = 2 / 51

        # ATR state
        self._atr14: float = 0.0
        self._atr_period: int = 14

        # Stochastic state
        self._stoch_period: int = 14
        self._stoch_k: float = 0.0
        self._stoch_d: float = 0.0  # 3-period SMA of K
        self._stoch_k_hist: List[float] = []

        self._initialized: bool = False
        self._prev_stoch_k: float = 0.0
        self._prev_stoch_d: float = 0.0

    def update(self, candle: CandleData) -> bool:
        """Update indicators with new 1H candle. Returns True when ready."""
        c, h, l, o = candle.close, candle.high, candle.low, candle.open

        self.closes.append(c)
        self.highs.append(h)
        self.lows.append(l)
        self.opens.append(o)
        self.volumes.append(candle.volume)

        n = len(self.closes)

        # Need at least 50 candles to compute EMA50 seed
        if n < 52:
            if n == 50:
                # Seed EMAs with SMA
                self._ema8 = np.mean(self.closes[-8:])
                self._ema21 = np.mean(self.closes[-21:])
                self._ema50 = np.mean(self.closes[-50:])
                # Seed ATR with average TR
                trs = []
                for i in range(1, n):
                    tr = max(
                        self.highs[i] - self.lows[i],
                        abs(self.highs[i] - self.closes[i-1]),
                        abs(self.lows[i] - self.closes[i-1])
                    )
                    trs.append(tr)
                self._atr14 = np.mean(trs[-14:]) if len(trs) >= 14 else np.mean(trs) if trs else 0
                # Seed stochastic
                self._compute_stoch()
            return n >= 52

        # Update EMAs
        self._ema8 = self._ema8 + self._ema8_k * (c - self._ema8)
        self._ema21 = self._ema21 + self._ema21_k * (c - self._ema21)
        self._ema50 = self._ema50 + self._ema50_k * (c - self._ema50)

        # Update ATR (Wilder smoothing)
        tr = max(h - l, abs(h - self.closes[-2]), abs(l - self.closes[-2]))
        self._atr14 = (self._atr14 * 13 + tr) / 14

        # Update stochastic
        self._prev_stoch_k = self._stoch_k
        self._prev_stoch_d = self._stoch_d
        self._compute_stoch()

        self._initialized = n >= 52
        return self._initialized

    def _compute_stoch(self):
        """Compute Stochastic K and D."""
        n = len(self.closes)
        period = min(self._stoch_period, n)
        recent_highs = self.highs[-period:]
        recent_lows = self.lows[-period:]
        hh = max(recent_highs)
        ll = min(recent_lows)
        denom = hh - ll
        if denom == 0:
            self._stoch_k = 50.0
        else:
            self._stoch_k = 100 * (self.closes[-1] - ll) / denom

        self._stoch_k_hist.append(self._stoch_k)
        if len(self._stoch_k_hist) >= 3:
            self._stoch_d = np.mean(self._stoch_k_hist[-3:])
        else:
            self._stoch_d = self._stoch_k

    @property
    def ema8(self) -> float: return self._ema8
    @property
    def ema21(self) -> float: return self._ema21
    @property
    def ema50(self) -> float: return self._ema50
    @property
    def atr14(self) -> float: return self._atr14
    @property
    def stoch_k(self) -> float: return self._stoch_k
    @property
    def stoch_d(self) -> float: return self._stoch_d
    @property
    def prev_stoch_k(self) -> float: return self._prev_stoch_k
    @property
    def prev_stoch_d(self) -> float: return self._prev_stoch_d


# Global indicator states per symbol
_indicator_states: Dict[str, IndicatorState] = {}


def get_indicator_state(symbol: str) -> IndicatorState:
    """Get or create indicator state for a symbol."""
    if symbol not in _indicator_states:
        _indicator_states[symbol] = IndicatorState()
    return _indicator_states[symbol]


def reset_indicator_state(symbol: str = ""):
    """Reset indicator state (for testing or pair rotation)."""
    if symbol:
        _indicator_states.pop(symbol, None)
    else:
        _indicator_states.clear()


# ==================================================================
#  SIGNAL DETECTION
# ==================================================================

STOCH_OVERSOLD = 30
STOCH_OVERBOUGHT = 70
BODY_MIN_RATIO = 0.4
ATR_SL_MULT = 0.4
RISK_FLOOR_PCT = 0.003
RISK_CAP_PCT = 0.05


def detect_nts_signal(
    symbol: str,
    candle: CandleData,
    state: IndicatorState,
) -> Optional[NTSSignal]:
    """
    Detect N_TREND_STOCH signal on the latest 1H candle.

    Long: EMA8>21>50, Stoch K crosses D from <30, bullish candle, body>40%
    Short: EMA8<21<50, Stoch K crosses D from >70, bearish candle, body>40%
    """
    if not state._initialized:
        return None

    c, o, h, l = candle.close, candle.open, candle.high, candle.low
    rng = h - l
    if rng == 0:
        return None

    body_ratio = abs(c - o) / rng
    atr = state.atr14
    if atr <= 0:
        return None

    # LONG signal
    if (state.ema8 > state.ema21 > state.ema50 and
        state.stoch_k > state.stoch_d and
        state.prev_stoch_k <= state.prev_stoch_d and
        state.stoch_k < STOCH_OVERSOLD and
        c > o and body_ratio > BODY_MIN_RATIO):

        entry = c
        sl = l - ATR_SL_MULT * atr
        risk = entry - sl
        if risk > 0 and RISK_FLOOR_PCT < risk / entry < RISK_CAP_PCT:
            return NTSSignal(
                symbol=symbol, direction="long", candle=candle,
                ema8=state.ema8, ema21=state.ema21, ema50=state.ema50,
                stoch_k=state.stoch_k, stoch_d=state.stoch_d,
                atr14=atr, signal_type=2,
            )

    # SHORT signal
    elif (state.ema8 < state.ema21 < state.ema50 and
          state.stoch_k < state.stoch_d and
          state.prev_stoch_k >= state.prev_stoch_d and
          state.stoch_k > STOCH_OVERBOUGHT and
          c < o and body_ratio > BODY_MIN_RATIO):

        entry = c
        sl = h + ATR_SL_MULT * atr
        risk = sl - entry
        if risk > 0 and RISK_FLOOR_PCT < risk / entry < RISK_CAP_PCT:
            return NTSSignal(
                symbol=symbol, direction="short", candle=candle,
                ema8=state.ema8, ema21=state.ema21, ema50=state.ema50,
                stoch_k=state.stoch_k, stoch_d=state.stoch_d,
                atr14=atr, signal_type=1,
            )

    return None


def scan_for_signal(
    symbol: str,
    candles: List[CandleData],
    require_confirmation: bool = False,
) -> Optional[NTSSignal]:
    """
    Backward-compatible scan interface.

    For N_TREND_STOCH, we process ALL candles through indicators
    and check the LAST candle for a signal.

    Stateless: recomputes indicators from scratch each call to avoid
    state corruption from repeated calls with overlapping candle data.
    """
    if len(candles) < 52:
        return None

    # Always recompute from scratch -- 60 candles is trivial
    state = IndicatorState()
    for c in candles[:-1]:
        state.update(c)

    # Update with latest candle
    latest = candles[-1]
    ready = state.update(latest)
    if not ready:
        return None

    # Store state for compute_trade's prev_candle lookup
    _indicator_states[symbol] = state

    return detect_nts_signal(symbol, latest, state)


# ==================================================================
#  TRADE COMPUTATION
# ==================================================================

def compute_trade(
    signal: NTSSignal,
    current_price: float,
    equity: float,
    risk_pct: float = RISK_PCT,
    tp_r: float = TP_R,
    leverage: int = LEVERAGE,
    exchange_tp_r: float = EXCHANGE_TP_R,
    min_notional: float = 5.0,
    min_qty: float = 0.001,
    price_precision: int = 2,
    qty_precision: int = 3,
    fixed_risk_usd: float = 0.0,
    max_positions: int = MAX_CONCURRENT_POSITIONS,
    available_balance: float = 0.0,
) -> Optional[TradeSignal]:
    """Compute trade parameters from an NTS signal."""
    sl = signal.stop_loss_price

    # Risk per unit = distance from entry to SL
    if signal.direction == "long":
        risk_per_unit = current_price - sl
    else:
        risk_per_unit = sl - current_price

    if risk_per_unit <= 0:
        return None
    if current_price > 0 and risk_per_unit / current_price < MIN_RISK_DISTANCE_PCT:
        return None

    # Dollar risk
    dollar_risk = fixed_risk_usd if fixed_risk_usd > 0 else equity * risk_pct

    # Position size
    qty = dollar_risk / risk_per_unit
    notional = qty * current_price

    # Margin safety cap
    if available_balance > 0:
        max_margin_per_trade = available_balance * 0.90
    else:
        max_margin_per_trade = equity * 0.90 / max(max_positions, 1)
    max_notional = max_margin_per_trade * leverage
    if notional > max_notional:
        qty = max_notional / current_price
        notional = qty * current_price
        dollar_risk = qty * risk_per_unit

    if notional < min_notional:
        return None
    if dollar_risk < 0.20:
        return None
    if qty < min_qty:
        return None

    # TP/SL
    if signal.direction == "long":
        tp = current_price + tp_r * risk_per_unit
        exchange_tp = current_price + exchange_tp_r * risk_per_unit
    else:
        tp = current_price - tp_r * risk_per_unit
        exchange_tp = current_price - exchange_tp_r * risk_per_unit

    fee_r = (FEE_RATE * 2 * current_price) / risk_per_unit

    # Create prev_candle from indicator state for logging
    state = get_indicator_state(signal.symbol)
    n = len(state.closes)
    if n >= 2:
        prev = CandleData(
            timestamp="", open=state.opens[-2], high=state.highs[-2],
            low=state.lows[-2], close=state.closes[-2], volume=state.volumes[-2])
    else:
        prev = signal.candle

    return TradeSignal(
        symbol=signal.symbol,
        direction=signal.direction,
        entry_price=current_price,
        stop_loss=sl,
        take_profit=tp,
        risk_per_unit=risk_per_unit,
        dollar_risk=dollar_risk,
        position_size=qty,
        fee_r=fee_r,
        exchange_tp=exchange_tp,
        ob_candle=signal.candle,
        prev_candle=prev,
    )


# ==================================================================
#  HTF/VOLUME FILTERS (kept from OBR for interface compat)
# ==================================================================

def compute_1h_trend(candles_5m: List[CandleData], sma_period: int = 50) -> int:
    """N_TREND_STOCH already runs on 1H -- trend is built into the EMAs."""
    return 0  # Not used; EMAs handle trend


def check_trend_alignment(signal_type: int, trend: int) -> bool:
    """Always True -- trend alignment is built into N_TREND_STOCH's EMA stack."""
    return True


def check_volume_spike(candles, ob_index=-2, lookback=20, threshold=2.0) -> bool:
    """Volume not required for N_TREND_STOCH (robustness test showed stable without it)."""
    return True


def check_nextbar_confirmation(signal_type: int, confirm_candle: CandleData) -> bool:
    """Not used for N_TREND_STOCH."""
    return True
