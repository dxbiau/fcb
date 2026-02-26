"""
obr/strategy.py -- Outside Bar Reversal signal detection & trade computation.

Pure functions -- no exchange calls, no side effects.
Data in, signals out.

Pattern (from SimpleCandleStrategy01 notebook):
  LONG signal (signal=2):
    - Current candle is BEARISH (open > close)
    - High > previous High  (engulfs upward)
    - Low < previous Low    (engulfs downward)
    - Close < previous Low  (extreme close beyond range)
    -> Enter LONG at next bar's open (fade the bearish exhaustion)

  SHORT signal (signal=1):
    - Current candle is BULLISH (open < close)
    - Low < previous Low    (engulfs downward)
    - High > previous High  (engulfs upward)
    - Close > previous High (extreme close beyond range)
    -> Enter SHORT at next bar's open (fade the bullish exhaustion)

SL: Outside bar's extreme (low for longs, high for shorts)
TP: Entry +/- TP_R * risk_per_unit
"""

from dataclasses import dataclass
from typing import Optional, List
from collections import deque
from obr.config import (
    TP_R, FEE_RATE, RISK_PCT, LEVERAGE, MAX_CONCURRENT_POSITIONS,
    MIN_RISK_DISTANCE_PCT, REQUIRE_NEXTBAR_CONFIRM, EXCHANGE_TP_R,
    SL_BUFFER_MULT,
    HTF_TREND_ENABLED, HTF_SMA_PERIOD, HTF_TREND_BUFFER,
    VOLUME_FILTER_ENABLED, VOLUME_SPIKE_THRESHOLD, VOLUME_LOOKBACK,
)


# ==================================================================
#  DATA CLASSES
# ==================================================================

@dataclass
class CandleData:
    """Minimal candle representation for signal detection."""
    timestamp: str       # ISO string or epoch
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
class OBRSignal:
    """Outside Bar Reversal signal."""
    symbol: str
    direction: str           # "long" or "short"
    ob_candle: CandleData    # the outside bar that generated the signal
    prev_candle: CandleData  # the candle before the OB
    signal_type: int         # 2=long, 1=short (matching notebook convention)

    @property
    def stop_loss_price(self) -> float:
        """SL at the OB candle's extreme."""
        if self.direction == "long":
            return self.ob_candle.low   # if it goes below OB low, reversal failed
        else:
            return self.ob_candle.high  # if it goes above OB high, reversal failed


@dataclass
class TradeSignal:
    """Computed trade ready for execution."""
    symbol: str
    direction: str         # "long" or "short"
    entry_price: float     # next bar's open (or current price for market order)
    stop_loss: float       # OB extreme
    take_profit: float     # entry +/- TP_R * risk
    risk_per_unit: float   # |entry - SL|
    dollar_risk: float     # equity * risk_pct
    position_size: float   # qty in base units
    fee_r: float           # estimated fee in R terms
    exchange_tp: float     # far-out TP for exchange (guardian manages real TP)
    ob_candle: CandleData  # for logging
    prev_candle: CandleData


# ==================================================================
#  SIGNAL DETECTION  (exact replica of notebook's total_signal)
# ==================================================================

def detect_outside_bar(prev: CandleData, current: CandleData) -> int:
    """
    Detect outside bar reversal signal.

    Exact replica of the notebook's total_signal() function:

    Returns:
        2 = bearish outside bar -> LONG reversal signal
        1 = bullish outside bar -> SHORT reversal signal
        0 = no signal
    """
    # LONG signal (signal=2): bearish outside bar
    # c0: Bearish candle (Open > Close)
    # c1: High > previous High
    # c2: Low < previous Low
    # c3: Close < previous Low (extreme close beyond prev range)
    if (current.open > current.close
            and current.high > prev.high
            and current.low < prev.low
            and current.close < prev.low):
        return 2

    # SHORT signal (signal=1): bullish outside bar
    # c0: Bullish candle (Open < Close)
    # c1: Low < previous Low
    # c2: High > previous High
    # c3: Close > previous High (extreme close beyond prev range)
    if (current.open < current.close
            and current.low < prev.low
            and current.high > prev.high
            and current.close > prev.high):
        return 1

    return 0


def check_nextbar_confirmation(signal_type: int, confirm_candle: CandleData) -> bool:
    """
    Check if the confirmation candle (bar after OB) confirms the reversal.

    For LONG (signal=2): confirmation candle must close above its open (bullish)
    For SHORT (signal=1): confirmation candle must close below its open (bearish)
    """
    if signal_type == 2:
        return confirm_candle.close > confirm_candle.open  # bullish confirmation
    elif signal_type == 1:
        return confirm_candle.close < confirm_candle.open  # bearish confirmation
    return False


# ==================================================================
#  HTF TREND ALIGNMENT FILTER
# ==================================================================

def compute_1h_trend(candles_5m: List[CandleData], sma_period: int = HTF_SMA_PERIOD) -> int:
    """
    Derive 1H trend from 5m candles using a simple moving average.
    
    Builds 1H candles by grouping every 12 consecutive 5m bars,
    then computes the SMA of the last `sma_period` 1H closes.
    
    Returns:
        1  = bullish (1H close > SMA) → only take longs
       -1  = bearish (1H close < SMA) → only take shorts
        0  = neutral / insufficient data → skip trade
    """
    if len(candles_5m) < 12 * sma_period:
        # Not enough data for full SMA — fall back to partial if we have >= 12 bars
        if len(candles_5m) < 24:
            return 0  # need at least 2 hours of data

    # Build 1H candles from 5m
    h1_closes = []
    bar_count = 0
    h1_close = 0.0
    for c in candles_5m:
        h1_close = c.close
        bar_count += 1
        if bar_count >= 12:
            h1_closes.append(h1_close)
            bar_count = 0

    if not h1_closes:
        return 0

    # Compute SMA over last sma_period 1H closes
    window = h1_closes[-sma_period:] if len(h1_closes) >= sma_period else h1_closes
    sma = sum(window) / len(window)
    last_close = h1_closes[-1]

    buf = HTF_TREND_BUFFER
    if last_close > sma * (1 + buf):
        return 1   # bullish
    elif last_close < sma * (1 - buf):
        return -1  # bearish
    return 0       # neutral — too close to SMA


def check_trend_alignment(signal_type: int, trend: int) -> bool:
    """
    Check if the OBR signal aligns with the 1H trend.
    
    signal_type 2 (long) requires bullish trend (1)
    signal_type 1 (short) requires bearish trend (-1)
    """
    if not HTF_TREND_ENABLED:
        return True  # filter disabled
    if signal_type == 2:
        return trend == 1
    if signal_type == 1:
        return trend == -1
    return False


# ==================================================================
#  VOLUME SPIKE FILTER
# ==================================================================

def check_volume_spike(
    candles: List[CandleData],
    ob_index: int = -2,
    lookback: int = VOLUME_LOOKBACK,
    threshold: float = VOLUME_SPIKE_THRESHOLD,
) -> bool:
    """
    Check if the outside bar candle has above-average volume.
    
    The OB candle is at candles[ob_index] (default: -2, the second-to-last).
    Volume must be >= threshold * average of the prior `lookback` candles.
    """
    if not VOLUME_FILTER_ENABLED:
        return True  # filter disabled

    n = len(candles)
    # Convert negative index to positive
    idx = n + ob_index if ob_index < 0 else ob_index
    
    if idx < lookback:
        return False  # not enough history
    
    # Compute average volume of lookback period before the OB candle
    vol_sum = 0.0
    for j in range(idx - lookback, idx):
        vol_sum += candles[j].volume
    avg_vol = vol_sum / lookback
    
    if avg_vol <= 0:
        return False

    return candles[idx].volume >= threshold * avg_vol


# ==================================================================
#  SIGNAL SCANNING
# ==================================================================

def scan_for_signal(
    symbol: str,
    candles: List[CandleData],  # newest last, at least 3 candles
    require_confirmation: bool = REQUIRE_NEXTBAR_CONFIRM,
) -> Optional[OBRSignal]:
    """
    Scan the last few candles for an OBR signal.

    In live mode with require_confirmation=True:
      - candles[-3] = the candle before the potential OB
      - candles[-2] = the potential outside bar
      - candles[-1] = the confirmation candle (just closed)

    If the OB is detected on candles[-2] and confirmation passes on candles[-1],
    we enter at the CURRENT price (which is effectively next bar's open).

    Without confirmation:
      - candles[-2] = the candle before the potential OB
      - candles[-1] = the potential outside bar (just closed)
      - Enter at next bar's open (current price)
    """
    if len(candles) < 2:
        return None

    if require_confirmation:
        if len(candles) < 3:
            return None
        prev = candles[-3]
        ob = candles[-2]
        confirm = candles[-1]

        signal_type = detect_outside_bar(prev, ob)
        if signal_type == 0:
            return None

        if not check_nextbar_confirmation(signal_type, confirm):
            return None

        direction = "long" if signal_type == 2 else "short"
        return OBRSignal(
            symbol=symbol,
            direction=direction,
            ob_candle=ob,
            prev_candle=prev,
            signal_type=signal_type,
        )
    else:
        prev = candles[-2]
        ob = candles[-1]

        signal_type = detect_outside_bar(prev, ob)
        if signal_type == 0:
            return None

        direction = "long" if signal_type == 2 else "short"
        return OBRSignal(
            symbol=symbol,
            direction=direction,
            ob_candle=ob,
            prev_candle=prev,
            signal_type=signal_type,
        )


# ==================================================================
#  TRADE COMPUTATION
# ==================================================================

def compute_trade(
    signal: OBRSignal,
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
    """
    Compute trade parameters from a signal.

    Entry: at current_price (market order after signal confirmed)
    SL: OB candle's extreme
    TP: entry +/- tp_r * risk_per_unit
    Size: fixed_risk_usd / risk_per_unit (or equity * risk_pct if no fixed risk)

    Includes margin safety cap: uses the exchange's available_balance (free
    margin not locked by open positions) when provided, otherwise falls back
    to equity / max_positions.
    """
    sl_raw = signal.stop_loss_price

    # Widen SL beyond OB extreme to survive noise wicks
    if signal.direction == "long":
        raw_dist = current_price - sl_raw
        sl = current_price - raw_dist * SL_BUFFER_MULT
    else:
        raw_dist = sl_raw - current_price
        sl = current_price + raw_dist * SL_BUFFER_MULT

    # Risk per unit = distance from entry to (widened) SL
    if signal.direction == "long":
        risk_per_unit = current_price - sl
    else:
        risk_per_unit = sl - current_price

    # Validate risk distance
    if risk_per_unit <= 0:
        return None
    if current_price > 0 and risk_per_unit / current_price < MIN_RISK_DISTANCE_PCT:
        return None

    # Dollar risk -- fixed USD if set, otherwise % of equity
    dollar_risk = fixed_risk_usd if fixed_risk_usd > 0 else equity * risk_pct

    # Position size (leveraged)
    qty = dollar_risk / risk_per_unit
    notional = qty * current_price

    # --- Margin safety cap ---
    # Use exchange's available (free) balance when provided — this already
    # excludes margin locked by open positions, so we don't over-allocate.
    # Keep 10% buffer so we never hit the exact limit.
    if available_balance > 0:
        max_margin_per_trade = available_balance * 0.90
    else:
        # Fallback: divide equity equally among all position slots
        max_margin_per_trade = equity * 0.90 / max(max_positions, 1)
    max_notional = max_margin_per_trade * leverage
    if notional > max_notional:
        qty = max_notional / current_price
        notional = qty * current_price
        dollar_risk = qty * risk_per_unit   # actual risk reduced

    # Check minimum notional
    if notional < min_notional:
        return None

    # Minimum risk threshold -- if margin cap shrinks risk below $0.20,
    # the trade is too small to justify fees.
    if dollar_risk < 0.20:
        return None

    # Check minimum qty (compare unrounded -- _execute_trade handles rounding)
    if qty < min_qty:
        return None

    # TP calculation
    if signal.direction == "long":
        tp = current_price + tp_r * risk_per_unit
        exchange_tp = current_price + exchange_tp_r * risk_per_unit
    else:
        tp = current_price - tp_r * risk_per_unit
        exchange_tp = current_price - exchange_tp_r * risk_per_unit

    # Fee estimate in R terms
    fee_r = (FEE_RATE * 2 * current_price) / risk_per_unit  # round-trip

    # NOTE: No rounding here -- _execute_trade uses ccxt's native
    # price_to_precision / amount_to_precision for exchange-correct values.

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
        ob_candle=signal.ob_candle,
        prev_candle=signal.prev_candle,
    )
