#!/usr/bin/env python3
"""
Traffic_lights_Yagee.py

Purpose
-------
A separate market + stock traffic-light decision engine.

Market layer
------------
Primary market: S&P 500 (^GSPC)
Secondary confirmation: QQQ

Four binary signals:
1) Close vs 21 EMA
2) 8 EMA vs 21 EMA
3) Parabolic SAR (step=0.02, max=0.20)
4) Chande Momentum Oscillator, 9-day (CMO > 0 bullish)

Raw classification:
- 3-4 green signals -> GREEN
- 2 green / 2 red -> YELLOW
- 0-1 green signals -> RED

Yellow persistence:
- When the primary market first becomes YELLOW, it is held YELLOW through
  the next decision window. Day 1 and Day 2 are locked YELLOW.
- On the 3rd trading session, the current raw signal is allowed to determine
  the new effective state.
- State is persisted in a JSON file beside this script.

Circuit breaker:
- If the S&P 500 is down 13% or more from the previous trading day's close,
  effective market status is forced to RED immediately, overriding Yellow lock.

Stock layer
-----------
Each requested stock/ETF gets the same four-signal raw traffic light.
The 3-day Yellow lock applies ONLY to the primary market layer, not individual stocks.

Combined action
---------------
- Market RED -> RED / DO NOT BUY OR ADD
- Market YELLOW + Stock RED -> RED / DO NOT ADD
- Market YELLOW + Stock GREEN/YELLOW -> YELLOW / CAUTION, HOLD/WATCH
- Market GREEN + Stock GREEN -> GREEN / BUY OR ADD (subject to entry setup/risk plan)
- Market GREEN + Stock YELLOW -> YELLOW / HOLD/WATCH
- Market GREEN + Stock RED -> RED / DO NOT ADD

Data source
-----------
Yahoo Finance via yfinance. This follows the same Yahoo-based market-data
convention used by the Investing project; the indicator layer is intentionally
separate from the existing entry/pivot logic.

Install
-------
python -m pip install yfinance pandas numpy

Run
---
python Traffic_lights_Yagee.py

Example input
-------------
MRVL, META, SOFI, AMZN, AAPL, PLTR
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import yfinance as yf


# =============================================================================
# Configuration
# =============================================================================

PRIMARY_MARKET = "^GSPC"
SECONDARY_MARKET = "QQQ"

EMA_FAST = 8
EMA_SLOW = 21
CMO_PERIOD = 9

PSAR_STEP = 0.02
PSAR_INCREMENT = 0.02
PSAR_MAX = 0.20

CIRCUIT_BREAKER_PCT = -13.0

# Day 1 and Day 2 remain locked Yellow; on Day 3 the current raw status may win.
YELLOW_DECISION_SESSION = 3

HISTORY_PERIOD = "9mo"
STATE_FILE = Path(__file__).with_name(".traffic_lights_yagee_state.json")


# =============================================================================
# Weighted secondary decision configuration
# =============================================================================

GROWTH_TECH_SEMICONDUCTOR = {
    "AAPL", "META", "AMZN", "GOOG", "GOOGL", "AMD", "NVDA", "MRVL",
    "MU", "QCOM", "PLTR", "NET", "TSLA", "NVTS", "ASML", "SOFI",
    "HOOD", "PYPL", "UBER"
}

GENERAL_NON_TECH = {
    "GE", "GEV", "LLY", "V", "AXP", "BN", "JNJ", "PEP", "HD", "NVO"
}

LIGHT_SCORE = {"GREEN": 1.0, "YELLOW": 0.0, "RED": -1.0}


# =============================================================================
# Models
# =============================================================================

@dataclass
class Signal:
    name: str
    green: bool
    value: str
    rule: str

    @property
    def light(self) -> str:
        return "🟢" if self.green else "🔴"


@dataclass
class TrafficResult:
    ticker: str
    date: str
    close: float
    ema8: float
    ema21: float
    psar: float
    cmo9: float
    signals: List[Signal]
    green_count: int
    red_count: int
    raw_status: str


# =============================================================================
# Data
# =============================================================================

def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    """Return clean OHLC data with a timezone-naive DatetimeIndex."""
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # yfinance can occasionally return MultiIndex columns.
    if isinstance(df.columns, pd.MultiIndex):
        # For a single ticker, use the first level that contains OHLC names.
        if {"Open", "High", "Low", "Close"}.issubset(set(df.columns.get_level_values(0))):
            df.columns = df.columns.get_level_values(0)
        elif {"Open", "High", "Low", "Close"}.issubset(set(df.columns.get_level_values(-1))):
            df.columns = df.columns.get_level_values(-1)

    needed = ["Open", "High", "Low", "Close"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    df = df[needed].copy()
    df = df.dropna(subset=["High", "Low", "Close"])

    idx = pd.to_datetime(df.index)
    try:
        idx = idx.tz_localize(None)
    except TypeError:
        pass
    df.index = idx

    return df


def download_ticker(ticker: str, period: str = HISTORY_PERIOD) -> pd.DataFrame:
    """Download daily OHLC history for one symbol."""
    df = yf.Ticker(ticker).history(
        period=period,
        interval="1d",
        auto_adjust=False,
        actions=False,
    )
    return normalize_history(df)


# =============================================================================
# Indicators
# =============================================================================

def calculate_cmo(close: pd.Series, period: int = CMO_PERIOD) -> pd.Series:
    """
    Chande Momentum Oscillator:
        100 * (sum(gains) - sum(losses)) / (sum(gains) + sum(losses))
    over the selected rolling period.
    """
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta.clip(upper=0.0))

    sum_gains = gains.rolling(period).sum()
    sum_losses = losses.rolling(period).sum()
    denom = sum_gains + sum_losses

    cmo = 100.0 * (sum_gains - sum_losses) / denom.replace(0, np.nan)
    return cmo.fillna(0.0)


def calculate_psar(
    df: pd.DataFrame,
    step: float = PSAR_STEP,
    max_step: float = PSAR_MAX,
    increment: float = PSAR_INCREMENT,
) -> Tuple[pd.Series, pd.Series]:
    """
    Standard Parabolic SAR implementation.

    Returns
    -------
    psar : pd.Series
        SAR value for each bar.
    bull : pd.Series[bool]
        True when the PSAR regime is bullish (SAR beneath price).
    """
    high = df["High"].astype(float).to_numpy()
    low = df["Low"].astype(float).to_numpy()
    close = df["Close"].astype(float).to_numpy()

    n = len(df)
    if n < 3:
        return (
            pd.Series(np.nan, index=df.index, name="psar"),
            pd.Series(False, index=df.index, name="psar_bull"),
        )

    psar = np.zeros(n, dtype=float)
    bull = np.ones(n, dtype=bool)

    # Initialize direction from early price movement.
    initial_bull = close[1] >= close[0]
    bull[0] = initial_bull
    bull[1] = initial_bull

    af = step
    if initial_bull:
        ep = max(high[0], high[1])
        psar[0] = low[0]
        psar[1] = min(low[0], low[1])
    else:
        ep = min(low[0], low[1])
        psar[0] = high[0]
        psar[1] = max(high[0], high[1])

    for i in range(2, n):
        prev_psar = psar[i - 1]
        current_bull = bull[i - 1]

        candidate = prev_psar + af * (ep - prev_psar)

        if current_bull:
            # SAR cannot be above the prior two lows.
            candidate = min(candidate, low[i - 1], low[i - 2])

            # Reversal: today's low crosses below SAR.
            if low[i] < candidate:
                current_bull = False
                candidate = ep
                ep = low[i]
                af = step
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + increment, max_step)

        else:
            # SAR cannot be below the prior two highs.
            candidate = max(candidate, high[i - 1], high[i - 2])

            # Reversal: today's high crosses above SAR.
            if high[i] > candidate:
                current_bull = True
                candidate = ep
                ep = high[i]
                af = step
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + increment, max_step)

        psar[i] = candidate
        bull[i] = current_bull

    return (
        pd.Series(psar, index=df.index, name="psar"),
        pd.Series(bull, index=df.index, name="psar_bull"),
    )


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema8"] = out["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    out["ema21"] = out["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    out["cmo9"] = calculate_cmo(out["Close"], CMO_PERIOD)
    out["psar"], out["psar_bull"] = calculate_psar(out)
    return out


# =============================================================================
# Four-light analysis
# =============================================================================

def classify_four_lights(green_count: int) -> str:
    if green_count >= 3:
        return "GREEN"
    if green_count == 2:
        return "YELLOW"
    return "RED"


def analyze_four_lights(ticker: str, df: pd.DataFrame) -> TrafficResult:
    if df.empty or len(df) < 30:
        raise ValueError(f"Not enough daily data for {ticker}")

    x = add_indicators(df)
    row = x.iloc[-1]

    close = float(row["Close"])
    low = float(row["Low"])
    high = float(row["High"])
    ema8 = float(row["ema8"])
    ema21 = float(row["ema21"])
    psar = float(row["psar"])
    cmo9 = float(row["cmo9"])
    psar_regime_bull = bool(row["psar_bull"])

    # 1. Closing bar above/below 21 EMA.
    s1_green = close > ema21

    # 2. Fast MA (8 EMA) above Slow MA (21 EMA).
    s2_green = ema8 > ema21

    # 3. PSAR underneath candle = bullish.
    # Use the PSAR regime, while reporting its geometric location too.
    s3_green = psar_regime_bull
    if psar < low:
        psar_location = "below candle"
    elif psar > high:
        psar_location = "above candle"
    else:
        psar_location = "inside candle/reversal bar"

    # 4. 9-day CMO above zero.
    # Exactly zero is treated as RED conservatively so every signal stays binary.
    s4_green = cmo9 > 0.0

    signals = [
        Signal(
            "Close vs 21 EMA",
            s1_green,
            f"Close {close:.2f} vs EMA21 {ema21:.2f}",
            "GREEN if Close > EMA21",
        ),
        Signal(
            "8 EMA vs 21 EMA",
            s2_green,
            f"EMA8 {ema8:.2f} vs EMA21 {ema21:.2f}",
            "GREEN if EMA8 > EMA21",
        ),
        Signal(
            "Parabolic SAR",
            s3_green,
            f"PSAR {psar:.2f} ({psar_location})",
            "GREEN when SAR regime is beneath price; step=.02, max=.20",
        ),
        Signal(
            "Chande CMO(9)",
            s4_green,
            f"CMO9 {cmo9:.2f}",
            "GREEN if CMO9 > 0",
        ),
    ]

    green_count = sum(int(s.green) for s in signals)
    raw_status = classify_four_lights(green_count)

    return TrafficResult(
        ticker=ticker,
        date=x.index[-1].date().isoformat(),
        close=close,
        ema8=ema8,
        ema21=ema21,
        psar=psar,
        cmo9=cmo9,
        signals=signals,
        green_count=green_count,
        red_count=4 - green_count,
        raw_status=raw_status,
    )


# =============================================================================
# Persistent Yellow state
# =============================================================================

def load_state() -> Dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: Dict) -> None:
    """
    Persist Yellow-lock state when local storage is writable.
    Hosted Streamlit filesystems may be read-only or ephemeral, so persistence
    failure must not crash the dashboard.
    """
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def trading_sessions_since(start_date: str, market_df: pd.DataFrame) -> int:
    """Count available S&P trading sessions from start_date through latest date, inclusive."""
    start = pd.Timestamp(start_date)
    dates = pd.DatetimeIndex(market_df.index).normalize().unique()
    return int(sum(d >= start.normalize() for d in dates))


def apply_yellow_lock(
    raw_status: str,
    market_date: str,
    market_df: pd.DataFrame,
    state: Dict,
) -> Tuple[str, Dict, str]:
    """
    Day 1 Yellow -> locked Yellow
    Day 2          -> locked Yellow
    Day 3          -> current raw status may determine effective status
    """
    yellow_start = state.get("yellow_start_date")

    if yellow_start:
        sessions = trading_sessions_since(yellow_start, market_df)

        if sessions < YELLOW_DECISION_SESSION:
            state["last_market_date"] = market_date
            state["last_raw_status"] = raw_status
            state["last_effective_status"] = "YELLOW"
            note = (
                f"YELLOW LOCK active: session {sessions}/{YELLOW_DECISION_SESSION}. "
                f"Raw status is {raw_status}, but effective status remains YELLOW."
            )
            return "YELLOW", state, note

        # Decision session reached: current raw condition is now allowed to win.
        if raw_status == "YELLOW":
            # Stay Yellow, but the confirmation wait has been satisfied.
            state["yellow_start_date"] = None
            note = (
                f"YELLOW decision session reached ({sessions}/{YELLOW_DECISION_SESSION}); "
                "raw market is still YELLOW."
            )
            effective = "YELLOW"
        else:
            state["yellow_start_date"] = None
            note = (
                f"YELLOW decision session reached ({sessions}/{YELLOW_DECISION_SESSION}); "
                f"effective market can now change to {raw_status}."
            )
            effective = raw_status

        state["last_market_date"] = market_date
        state["last_raw_status"] = raw_status
        state["last_effective_status"] = effective
        return effective, state, note

    # No active Yellow lock.
    if raw_status == "YELLOW":
        state["yellow_start_date"] = market_date
        effective = "YELLOW"
        note = (
            f"New YELLOW period started on {market_date}. "
            f"Day 1 of {YELLOW_DECISION_SESSION}; Yellow lock is now active."
        )
    else:
        effective = raw_status
        note = "No Yellow lock is active."

    state["last_market_date"] = market_date
    state["last_raw_status"] = raw_status
    state["last_effective_status"] = effective
    return effective, state, note


# =============================================================================
# Circuit breaker
# =============================================================================

def calculate_sp500_daily_change(market_df: pd.DataFrame) -> Tuple[float, float, float]:
    if len(market_df) < 2:
        raise ValueError("Need at least two S&P 500 daily bars for circuit breaker.")

    previous_close = float(market_df["Close"].iloc[-2])
    latest_price = float(market_df["Close"].iloc[-1])
    change_pct = ((latest_price - previous_close) / previous_close) * 100.0
    return previous_close, latest_price, change_pct


# =============================================================================
# Ticker classification + weighted secondary decision
# =============================================================================

def classify_ticker_for_weights(
    ticker: str,
    profiles: Dict[str, Dict[str, float]],
) -> Tuple[str, Dict[str, float], str]:
    """
    Classify a ticker, then use the server-supplied proprietary profile.
    Exact profile values are intentionally not stored in this source file.
    """
    ticker = ticker.upper().strip()

    if ticker == "SPY":
        return "S&P ETF", profiles["spy"].copy(), "ETF special-case"
    if ticker == "VOO":
        return "S&P ETF", profiles["voo"].copy(), "ETF special-case"
    if ticker == "QQQ":
        return "QQQ ETF", profiles["qqq_etf"].copy(), "ETF special-case"

    if ticker in GROWTH_TECH_SEMICONDUCTOR:
        return "Growth/Tech/Semiconductor", profiles["growth"].copy(), "explicit watchlist"

    if ticker in GENERAL_NON_TECH:
        return "General/Non-tech", profiles["general"].copy(), "explicit watchlist"

    try:
        info = yf.Ticker(ticker).get_info()
        sector = str(info.get("sector", "") or "").strip()
        industry = str(info.get("industry", "") or "").strip()
        quote_type = str(info.get("quoteType", "") or "").upper()

        if quote_type in {"ETF", "MUTUALFUND"}:
            return "Other ETF", profiles["other_etf"].copy(), "Yahoo quote type"

        growth_sectors = {"Technology", "Communication Services", "Consumer Cyclical"}
        growth_terms = ("semiconductor", "software", "internet", "electronics")

        if sector in growth_sectors or any(term in industry.lower() for term in growth_terms):
            return "Growth/Tech/Semiconductor", profiles["growth"].copy(), f"Yahoo sector: {sector or industry}"

        if sector:
            return "General/Non-tech", profiles["general"].copy(), f"Yahoo sector: {sector}"

    except Exception:
        pass

    return "General/Non-tech", profiles["general"].copy(), "fallback default"


def raw_weighted_score(
    sp500_status: str,
    qqq_status: str,
    stock_status: str,
    weights: Dict[str, float],
) -> float:
    return (
        weights["sp500"] * LIGHT_SCORE[sp500_status]
        + weights["qqq"] * LIGHT_SCORE[qqq_status]
        + weights["stock"] * LIGHT_SCORE[stock_status]
    )


def classify_weighted_score(
    score: float,
    green_threshold: float,
    red_threshold: float,
) -> str:
    if score >= green_threshold:
        return "GREEN"
    if score <= red_threshold:
        return "RED"
    return "YELLOW"


def weighted_action(
    effective_market: str,
    qqq_status: str,
    stock_status: str,
    weights: Dict[str, float],
    green_threshold: float,
    red_threshold: float,
    circuit_breaker: bool = False,
) -> Tuple[str, float, str]:
    score = raw_weighted_score(effective_market, qqq_status, stock_status, weights)
    preliminary = classify_weighted_score(score, green_threshold, red_threshold)

    if circuit_breaker:
        return "RED", score, "Circuit breaker override"
    if effective_market == "RED":
        return "RED", score, "S&P market RED safety gate"
    if stock_status == "RED":
        return "RED", score, "Individual stock RED safety gate"
    if effective_market == "YELLOW":
        return "YELLOW", score, "Effective S&P YELLOW caps result at YELLOW"

    return preliminary, score, "Weighted score"


# =============================================================================
# Combined decision
# =============================================================================

def combined_action(market_status: str, stock_status: str) -> Tuple[str, str]:
    """
    Return (overall traffic light, action text).

    QQQ is confirmation-only and has no direct weight in this calculation.
    This is a hierarchical decision matrix, not a numerical weighted average:
    the effective S&P 500 market light is the risk-permission gate, and the
    individual ticker light determines whether that ticker is healthy enough
    to use that market permission.
    """
    if market_status == "RED":
        return "RED", "DO NOT BUY OR ADD; defensive/cash posture."

    if market_status == "YELLOW":
        if stock_status == "RED":
            return "RED", "DO NOT ADD; stock is weak while market is transitional."
        return "YELLOW", "CAUTION: HOLD/WATCH; wait for market transition to resolve."

    # Market GREEN
    if stock_status == "GREEN":
        return "GREEN", "BUY/ADD is supported, subject to your separate entry setup and risk plan."
    if stock_status == "YELLOW":
        return "YELLOW", "HOLD/WATCH; market supports risk but the stock is transitional."
    return "RED", "DO NOT ADD; individual stock trend is weak despite a Green market."


# =============================================================================
# Output
# =============================================================================

STATUS_ICON = {
    "GREEN": "🟢",
    "YELLOW": "🟡",
    "RED": "🔴",
}


def print_signal_breakdown(title: str, result: TrafficResult) -> None:
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)
    print(
        f"{result.ticker} | Date: {result.date} | Close: {result.close:.2f} | "
        f"Raw: {STATUS_ICON[result.raw_status]} {result.raw_status} "
        f"({result.green_count} green / {result.red_count} red)"
    )
    print("-" * 100)

    rows = []
    for idx, s in enumerate(result.signals, start=1):
        rows.append(
            {
                "#": idx,
                "Light": s.light,
                "Indicator": s.name,
                "Observed": s.value,
                "Rule": s.rule,
            }
        )
    print(pd.DataFrame(rows).to_string(index=False))


def print_summary_table(rows: List[Dict]) -> None:
    if not rows:
        return

    print("\n" + "=" * 190)
    print("OVERALL ACTION SUMMARY")
    print("=" * 190)

    header = (
        f"{'Ticker':<9}"
        f"{'Class':<26}"
        f"{'Market':<9}"
        f"{'QQQ':<9}"
        f"{'Stock':<9}"
        f"{'Signals':<9}"
        f"{'Non-Wtd':<10}"
        f"{'Weighted':<10}"
        f"{'Score':<8}"
        f"{'Weights S/Q/Stk':<18}"
        f"Action"
    )
    print(header)
    print("-" * 190)

    for row in rows:
        print(
            f"{str(row.get('Ticker', '')):<9}"
            f"{str(row.get('Class', ''))[:24]:<26}"
            f"{str(row.get('Market', '')):<9}"
            f"{str(row.get('QQQ', '')):<9}"
            f"{str(row.get('Stock Light', '')):<9}"
            f"{str(row.get('Signals', '')):<9}"
            f"{str(row.get('Non-Weighted', '')):<10}"
            f"{str(row.get('Weighted', '')):<10}"
            f"{str(row.get('Weighted Score', '')):<8}"
            f"{str(row.get('Weights', '')):<18}"
            f"{row.get('Action', '')}"
        )

# =============================================================================
# Main
# =============================================================================

def main() -> None:
    print("\nTraffic Lights - Yagee")
    print("Primary market: S&P 500 (^GSPC) | Secondary confirmation: QQQ")
    print("Enter one or more symbols separated by commas.")
    raw = input("Symbols: ").strip()

    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    symbols = list(dict.fromkeys(symbols))

    if not symbols:
        print("No symbols entered.")
        return

    # ---------------------------------------------------------
    # Primary market
    # ---------------------------------------------------------
    print("\nDownloading primary market data...")
    sp500_df = download_ticker(PRIMARY_MARKET)
    sp500_result = analyze_four_lights(PRIMARY_MARKET, sp500_df)

    prev_close, latest_price, day_change_pct = calculate_sp500_daily_change(sp500_df)
    circuit_breaker = day_change_pct <= CIRCUIT_BREAKER_PCT

    state = load_state()

    if circuit_breaker:
        effective_market = "RED"
        yellow_note = (
            f"CIRCUIT BREAKER OVERRIDE: S&P 500 change is {day_change_pct:.2f}% "
            f"from previous close ({prev_close:.2f} -> {latest_price:.2f}), "
            f"which is <= {CIRCUIT_BREAKER_PCT:.1f}%."
        )
        # Circuit breaker overrides, but preserve any Yellow start date so a later run
        # still has an audit trail. Effective state is forced RED.
        state["last_market_date"] = sp500_result.date
        state["last_raw_status"] = sp500_result.raw_status
        state["last_effective_status"] = "RED"
        state["last_circuit_breaker"] = True
    else:
        effective_market, state, yellow_note = apply_yellow_lock(
            sp500_result.raw_status,
            sp500_result.date,
            sp500_df,
            state,
        )
        state["last_circuit_breaker"] = False

    save_state(state)

    print_signal_breakdown("PRIMARY MARKET TRAFFIC LIGHT", sp500_result)

    print("\nPrimary Market Decision")
    print("-" * 100)
    print(f"S&P 500 previous close : {prev_close:.2f}")
    print(f"S&P 500 latest price   : {latest_price:.2f}")
    print(f"Daily change           : {day_change_pct:.2f}%")
    print(f"Raw market status      : {STATUS_ICON[sp500_result.raw_status]} {sp500_result.raw_status}")
    print(f"Effective market       : {STATUS_ICON[effective_market]} {effective_market}")
    print(f"State logic            : {yellow_note}")

    # ---------------------------------------------------------
    # Secondary QQQ confirmation
    # ---------------------------------------------------------
    print("\nDownloading QQQ confirmation data...")
    qqq_df = download_ticker(SECONDARY_MARKET)
    qqq_result = analyze_four_lights(SECONDARY_MARKET, qqq_df)
    print_signal_breakdown("SECONDARY GROWTH / TECH CONFIRMATION", qqq_result)

    if qqq_result.raw_status != effective_market:
        print(
            f"\nNOTE: Primary S&P 500 effective market is {effective_market}, "
            f"while QQQ raw confirmation is {qqq_result.raw_status}. "
            "QQQ is confirmation-only for the preserved non-weighted method; it has explicit weight in the secondary weighted method."
        )

    # ---------------------------------------------------------
    # Requested stocks
    # ---------------------------------------------------------
    summary_rows: List[Dict] = []

    for symbol in symbols:
        try:
            print(f"\nDownloading {symbol}...")
            df = download_ticker(symbol)
            result = analyze_four_lights(symbol, df)

            print_signal_breakdown(f"INDIVIDUAL STOCK / ETF TRAFFIC LIGHT: {symbol}", result)

            nonweighted_light, nonweighted_action_text = combined_action(
                effective_market, result.raw_status
            )

            classification, weights, class_source = classify_ticker_for_weights(symbol)
            weighted_light, weighted_score, weighted_reason = weighted_action(
                effective_market=effective_market,
                qqq_status=qqq_result.raw_status,
                stock_status=result.raw_status,
                weights=weights,
                circuit_breaker=circuit_breaker,
            )

            weights_text = (
                f"{int(weights['sp500'] * 100)}/"
                f"{int(weights['qqq'] * 100)}/"
                f"{int(weights['stock'] * 100)}"
            )

            print("\nCombined Decision")
            print("-" * 100)
            print(f"Market               : {STATUS_ICON[effective_market]} {effective_market}")
            print(f"QQQ confirm          : {STATUS_ICON[qqq_result.raw_status]} {qqq_result.raw_status}")
            print(f"{symbol:<20} : {STATUS_ICON[result.raw_status]} {result.raw_status}")
            print(f"Ticker classification: {classification} ({class_source})")
            print(f"Weights S&P/QQQ/Stock: {weights_text}%")
            print(f"Non-weighted Overall : {STATUS_ICON[nonweighted_light]} {nonweighted_light}")
            print(f"Weighted score       : {weighted_score:+.2f}")
            print(f"Weighted Overall     : {STATUS_ICON[weighted_light]} {weighted_light}")
            print(f"Weighted reason      : {weighted_reason}")
            print(f"Non-weighted action  : {nonweighted_action_text}")

            summary_rows.append(
                {
                    "Ticker": symbol,
                    "Class": classification,
                    "Market": effective_market,
                    "QQQ": qqq_result.raw_status,
                    "Stock Light": result.raw_status,
                    "Signals": f"{result.green_count}G/{result.red_count}R",
                    "Non-Weighted": nonweighted_light,
                    "Weighted": weighted_light,
                    "Weighted Score": f"{weighted_score:+.2f}",
                    "Weights": weights_text,
                    "Action": nonweighted_action_text,
                }
            )

        except Exception as exc:
            summary_rows.append(
                {
                    "Ticker": symbol,
                    "Class": "ERROR",
                    "Market": effective_market,
                    "QQQ": qqq_result.raw_status,
                    "Stock Light": "ERROR",
                    "Signals": "-",
                    "Non-Weighted": "ERROR",
                    "Weighted": "ERROR",
                    "Weighted Score": "-",
                    "Weights": "-",
                    "Action": str(exc),
                }
            )


    print("\nState file:")
    print(STATE_FILE.resolve())
    print("\nDone.\n")


if __name__ == "__main__":
    main()
