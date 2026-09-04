from __future__ import annotations

import json
import re
import requests
from pathlib import Path
from typing import Dict, List

import pandas as pd
import streamlit as st

import traffic_lights_core as tl


# =============================================================================
# App configuration
# =============================================================================

st.set_page_config(
    page_title="Market & Stock Traffic Lights",
    page_icon="🚦",
    layout="wide",
)

SIGNUPS_FILE = Path(__file__).with_name("traffic_light_signups.json")

# Fixed universe. Custom symbols entered by a user are session-only and do not
# modify this list.
UNIVERSE = sorted(
    {
        "AAPL",
        "AMD",
        "AMZN",
        "ASML",
        "AXP",
        "BN",
        "GE",
        "GOOG",
        "HOOD",
        "LLY",
        "META",
        "MRVL",
        "MU",
        "NET",
        "NVTS",
        "PLTR",
        "PYPL",
        "QCOM",
        "QQQ",
        "SOFI",
        "SPY",
        "TSLA",
        "UBER",
        "V",
        "VOO",
    }
)

STATUS_ICON = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}
STATUS_BG = {
    "GREEN": "#1f8f4e",
    "YELLOW": "#d6a700",
    "RED": "#c63b3b",
}
STATUS_FG = {"GREEN": "white", "YELLOW": "black", "RED": "white"}


# =============================================================================
# Authentication, proprietary server-side configuration, and persistence
# =============================================================================

def _secret_value(section: str, key: str):
    """Read a required Streamlit secret without exposing its value."""
    try:
        return st.secrets[section][key]
    except Exception as exc:
        raise RuntimeError(
            f"Server configuration is incomplete: missing [{section}] {key}."
        ) from exc


def load_proprietary_config() -> Dict:
    """
    Load proprietary weights/thresholds from Streamlit server-side Secrets.
    Exact values are intentionally absent from the repository source.
    """
    p = st.secrets["proprietary"]

    profiles = {
        "growth": {
            "sp500": float(p["growth_sp500"]),
            "qqq": float(p["growth_qqq"]),
            "stock": float(p["growth_stock"]),
        },
        "general": {
            "sp500": float(p["general_sp500"]),
            "qqq": float(p["general_qqq"]),
            "stock": float(p["general_stock"]),
        },
        "spy": {
            "sp500": float(p["spy_sp500"]),
            "qqq": float(p["spy_qqq"]),
            "stock": float(p["spy_stock"]),
        },
        "voo": {
            "sp500": float(p["voo_sp500"]),
            "qqq": float(p["voo_qqq"]),
            "stock": float(p["voo_stock"]),
        },
        "qqq_etf": {
            "sp500": float(p["qqq_sp500"]),
            "qqq": float(p["qqq_qqq"]),
            "stock": float(p["qqq_stock"]),
        },
        "other_etf": {
            "sp500": float(p["other_etf_sp500"]),
            "qqq": float(p["other_etf_qqq"]),
            "stock": float(p["other_etf_stock"]),
        },
    }

    for profile_name, weights in profiles.items():
        total = sum(weights.values())
        if abs(total - 1.0) > 1e-6:
            raise RuntimeError(
                f"Server configuration error: proprietary profile '{profile_name}' "
                "must sum to 1.0."
            )

    return {
        "profiles": profiles,
        "green_threshold": float(p["green_threshold"]),
        "red_threshold": float(p["red_threshold"]),
    }


def oidc_login_gate() -> str:
    """
    Single Google OIDC login. Streamlit stores the identity cookie and exposes
    the verified Google identity through st.user.
    """
    try:
        logged_in = bool(st.user.is_logged_in)
    except Exception:
        st.error(
            "Google sign-in is not configured yet. Configure the [auth] section "
            "in Streamlit Secrets before using this deployment."
        )
        st.stop()

    if not logged_in:
        st.title("🚦 Market & Stock Traffic Lights")
        st.write("Sign in once with Google to continue.")
        if st.button("Sign in with Google", type="primary"):
            st.login()
        st.stop()

    email = str(getattr(st.user, "email", "") or "").strip().lower()
    email_verified = bool(getattr(st.user, "email_verified", False))

    if not email or not email_verified:
        st.error("A verified Google email address is required.")
        if st.button("Sign out"):
            st.logout()
        st.stop()

    return email


def supabase_headers() -> Dict[str, str]:
    key = str(_secret_value("supabase", "service_role_key"))
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def supabase_url(path: str) -> str:
    base = str(_secret_value("supabase", "url")).rstrip("/")
    return f"{base}/rest/v1/{path.lstrip('/')}"


def get_user_access_status(email: str) -> str:
    """
    Return pending/approved/denied. A first-time Google user is automatically
    recorded as pending for administrator review in Supabase.
    """
    params = {
        "email": f"eq.{email}",
        "select": "email,status",
        "limit": "1",
    }
    r = requests.get(
        supabase_url("app_users"),
        headers=supabase_headers(),
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()

    if rows:
        return str(rows[0].get("status", "pending")).lower()

    name = str(getattr(st.user, "name", "") or "")
    payload = {"email": email, "name": name, "status": "pending"}
    r = requests.post(
        supabase_url("app_users"),
        headers={**supabase_headers(), "Prefer": "return=minimal"},
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return "pending"


def access_approval_gate(email: str) -> None:
    try:
        status = get_user_access_status(email)
    except Exception as exc:
        st.error("The user-access service is unavailable.")
        st.caption(str(exc))
        st.stop()

    if status == "approved":
        return

    st.title("🚦 Market & Stock Traffic Lights")

    if status == "denied":
        st.error("Access to this app has not been approved for this account.")
    else:
        st.warning(
            "Your Google account is verified, but access is awaiting administrator approval."
        )
        st.write(f"Signed in as **{email}**")
        if st.button("Check access again"):
            st.rerun()

    if st.button("Sign out"):
        st.logout()
    st.stop()


def load_saved_symbols(email: str) -> set[str]:
    params = {
        "user_email": f"eq.{email}",
        "select": "symbol",
        "order": "symbol.asc",
    }
    r = requests.get(
        supabase_url("user_symbols"),
        headers=supabase_headers(),
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return {
        str(row["symbol"]).strip().upper()
        for row in r.json()
        if row.get("symbol")
    }


def save_symbol_for_user(email: str, symbol: str) -> None:
    symbol = symbol.strip().upper()
    payload = {"user_email": email, "symbol": symbol}
    headers = {
        **supabase_headers(),
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    r = requests.post(
        supabase_url("user_symbols"),
        headers=headers,
        json=payload,
        timeout=15,
    )
    r.raise_for_status()


# =============================================================================
# Cached analysis
# =============================================================================

@st.cache_data(ttl=900, show_spinner=False)
def get_history(ticker: str) -> pd.DataFrame:
    return tl.download_ticker(ticker)


@st.cache_data(ttl=900, show_spinner=False)
def get_four_light_result(ticker: str):
    return tl.analyze_four_lights(ticker, get_history(ticker))


@st.cache_data(ttl=900, show_spinner=False)
def get_company_name(ticker: str) -> str:
    """Return Yahoo's full/long company name when available."""
    try:
        info = tl.yf.Ticker(ticker).get_info()
        return (
            str(info.get("longName") or info.get("shortName") or ticker).strip()
        )
    except Exception:
        return ticker


def get_market_context() -> Dict:
    sp500_df = get_history(tl.PRIMARY_MARKET)
    sp500_result = tl.analyze_four_lights(tl.PRIMARY_MARKET, sp500_df)

    prev_close, latest_price, day_change_pct = tl.calculate_sp500_daily_change(sp500_df)
    circuit_breaker = day_change_pct <= tl.CIRCUIT_BREAKER_PCT

    state = tl.load_state()

    if circuit_breaker:
        effective_market = "RED"
        state_note = (
            f"CIRCUIT BREAKER OVERRIDE: S&P 500 is {day_change_pct:.2f}% from "
            f"the previous close. Threshold = {tl.CIRCUIT_BREAKER_PCT:.1f}%."
        )
        state["last_market_date"] = sp500_result.date
        state["last_raw_status"] = sp500_result.raw_status
        state["last_effective_status"] = "RED"
        state["last_circuit_breaker"] = True
    else:
        effective_market, state, state_note = tl.apply_yellow_lock(
            sp500_result.raw_status,
            sp500_result.date,
            sp500_df,
            state,
        )
        state["last_circuit_breaker"] = False

    tl.save_state(state)

    qqq_result = get_four_light_result(tl.SECONDARY_MARKET)

    return {
        "sp500_df": sp500_df,
        "sp500_result": sp500_result,
        "prev_close": prev_close,
        "latest_price": latest_price,
        "day_change_pct": day_change_pct,
        "circuit_breaker": circuit_breaker,
        "effective_market": effective_market,
        "state_note": state_note,
        "qqq_result": qqq_result,
    }


def action_from_weighted(weighted_status: str) -> str:
    if weighted_status == "GREEN":
        return "BUY/ADD is supported, subject to your separate entry setup and risk plan."
    if weighted_status == "YELLOW":
        return "CAUTION: HOLD/WATCH; wait for stronger confirmation before adding."
    return "DO NOT BUY OR ADD; defensive/cash posture."


def analyze_symbol(symbol: str, context: Dict) -> Dict:
    symbol = symbol.upper().strip()
    result = get_four_light_result(symbol)

    nonweighted_light, _ = tl.combined_action(
        context["effective_market"],
        result.raw_status,
    )

    proprietary = load_proprietary_config()
    classification, weights, class_source = tl.classify_ticker_for_weights(
        symbol,
        proprietary["profiles"],
    )
    weighted_light, weighted_score, weighted_reason = tl.weighted_action(
        effective_market=context["effective_market"],
        qqq_status=context["qqq_result"].raw_status,
        stock_status=result.raw_status,
        weights=weights,
        green_threshold=proprietary["green_threshold"],
        red_threshold=proprietary["red_threshold"],
        circuit_breaker=context["circuit_breaker"],
    )

    return {
        "symbol": symbol,
        "company_name": get_company_name(symbol),
        "result": result,
        "classification": classification,
        "class_source": class_source,
        "weights": weights,
        "nonweighted_light": nonweighted_light,
        "weighted_light": weighted_light,
        "weighted_score": weighted_score,
        "weighted_reason": weighted_reason,
        "action": action_from_weighted(weighted_light),
    }


# =============================================================================
# UI helpers
# =============================================================================

def signal_table(result) -> pd.DataFrame:
    rows = []
    for i, sig in enumerate(result.signals, start=1):
        rows.append(
            {
                "#": i,
                "Light": sig.light,
                "Indicator": sig.name,
                "Observed": sig.value,
                "Rule": sig.rule,
            }
        )
    return pd.DataFrame(rows)


def status_badge(label: str, status: str) -> str:
    bg = STATUS_BG[status]
    fg = STATUS_FG[status]
    return (
        f"<span style='display:inline-block;padding:0.35rem 0.65rem;"
        f"border-radius:0.5rem;background:{bg};color:{fg};font-weight:700;'>"
        f"{label}: {STATUS_ICON[status]} {status}</span>"
    )


def render_symbol_buttons(analyses: Dict[str, Dict], key_prefix: str = "universe") -> None:
    """
    Render symbols in a 3-column grid, alphabetically row by row.

    Native Streamlit buttons preserve session_state/authentication.
    """
    symbols = sorted(analyses.keys())

    for row_start in range(0, len(symbols), 3):
        cols = st.columns(3)
        for offset, col in enumerate(cols):
            idx = row_start + offset
            if idx >= len(symbols):
                continue

            symbol = symbols[idx]
            status = analyses[symbol]["weighted_light"]

            with col:
                if st.button(
                    f"{STATUS_ICON[status]} {symbol}",
                    key=f"{key_prefix}_{symbol}",
                    use_container_width=True,
                ):
                    st.session_state["show_details_for"] = symbol
                    st.rerun()


def render_combined(detail: Dict, context: Dict) -> None:
    result = detail["result"]

    st.markdown("## Combined Decision")
    st.markdown(
        f"**{detail['symbol']} — {detail['company_name']}**"
    )

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            status_badge("Smart Proprietary Method", detail["weighted_light"]),
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            status_badge("Y Method", detail["nonweighted_light"]),
            unsafe_allow_html=True,
        )

    rows = {
        "Market": f"{STATUS_ICON[context['effective_market']]} {context['effective_market']}",
        "QQQ confirm": f"{STATUS_ICON[context['qqq_result'].raw_status]} {context['qqq_result'].raw_status}",
        f"{detail['symbol']}": f"{STATUS_ICON[result.raw_status]} {result.raw_status}",
    }

    for label, value in rows.items():
        st.markdown(f"**{label}:** {value}")

    action_box_bg = {
        "GREEN": "#d8f3dc",
        "YELLOW": "#fff3bf",
        "RED": "#ffd6d6",
    }[detail["weighted_light"]]

    action_box_border = {
        "GREEN": "#2d6a4f",
        "YELLOW": "#b08900",
        "RED": "#b02a37",
    }[detail["weighted_light"]]

    st.markdown(
        f"""
        <div style="
            margin-top: 1rem;
            margin-bottom: 1rem;
            padding: 1.15rem 1.25rem;
            border-radius: 0.75rem;
            border: 2px solid {action_box_border};
            background-color: {action_box_bg};
            color: #111111;
            text-align: center;
        ">
            <div style="font-size: 1.65rem; font-weight: 800; margin-bottom: 0.45rem;">
                ACTION:
            </div>
            <div style="font-size: 1.05rem; font-weight: 600;">
                {detail["action"]}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if context["circuit_breaker"]:
        st.error(
            f"🚨 CIRCUIT BREAKER OVERRIDE — S&P 500 declined "
            f"{abs(context['day_change_pct']):.2f}% from the previous close, "
            f"meeting or exceeding the {abs(tl.CIRCUIT_BREAKER_PCT):.0f}% override threshold. "
            "Normal traffic-light and Yellow-lock logic has been overridden. "
            "Overall action is RED / defensive."
        )


def render_market_section(context: Dict) -> None:
    result = context["sp500_result"]

    with st.expander("Primary Market: S&P 500 (^GSPC)", expanded=False):
        st.markdown(
            status_badge("Effective market", context["effective_market"]),
            unsafe_allow_html=True,
        )
        st.write(
            f"Raw market: {STATUS_ICON[result.raw_status]} **{result.raw_status}**  "
            f"({result.green_count} green / {result.red_count} red)"
        )
        st.write(f"Date: **{result.date}** | Close: **{result.close:.2f}**")
        st.write(
            f"Previous close: **{context['prev_close']:.2f}** | "
            f"Daily change: **{context['day_change_pct']:.2f}%** | "
            f"Circuit breaker: **{'TRIGGERED' if context['circuit_breaker'] else 'Not triggered'}** "
            f"(threshold {tl.CIRCUIT_BREAKER_PCT:.1f}%)"
        )
        st.info(context["state_note"])
        st.dataframe(signal_table(result), hide_index=True, use_container_width=True)


def render_qqq_section(context: Dict) -> None:
    result = context["qqq_result"]

    with st.expander("Secondary Support: QQQ", expanded=False):
        st.markdown(
            status_badge("QQQ", result.raw_status),
            unsafe_allow_html=True,
        )
        st.write(
            f"Date: **{result.date}** | Close: **{result.close:.2f}** | "
            f"{result.green_count} green / {result.red_count} red"
        )
        st.dataframe(signal_table(result), hide_index=True, use_container_width=True)


def render_stock_section(detail: Dict) -> None:
    result = detail["result"]
    symbol = detail["symbol"]

    with st.expander(f"Individual Stock: {symbol}", expanded=False):
        st.markdown(
            status_badge(symbol, result.raw_status),
            unsafe_allow_html=True,
        )
        st.write(
            f"Date: **{result.date}** | Close: **{result.close:.2f}** | "
            f"{result.green_count} green / {result.red_count} red"
        )
        st.dataframe(signal_table(result), hide_index=True, use_container_width=True)


# =============================================================================
# Main app
# =============================================================================
user_email = oidc_login_gate()
access_approval_gate(user_email)

try:
    saved_symbols = load_saved_symbols(user_email)
except Exception as exc:
    st.error("Could not load your saved universe.")
    st.caption(str(exc))
    saved_symbols = set()

current_universe = sorted(set(UNIVERSE) | saved_symbols)

st.title("🚦 Market & Stock Traffic Lights")
st.caption(
    "Traffic-light output is a decision-support tool, not a guarantee of market direction "
    "or investment outcome."
)

with st.sidebar:
    st.caption(f"Signed in as {user_email}")
    if st.button("Sign out"):
        st.logout()

with st.spinner("Refreshing market and stock traffic lights..."):
    context = get_market_context()

    analyses: Dict[str, Dict] = {}
    errors: Dict[str, str] = {}
    for symbol in current_universe:
        try:
            analyses[symbol] = analyze_symbol(symbol, context)
        except Exception as exc:
            errors[symbol] = str(exc)

st.markdown("### Current universe")
render_symbol_buttons(analyses, key_prefix="universe")

custom = st.text_input(
    "Other stock symbol(s)",
    value=st.session_state.get("custom_symbols", ""),
    placeholder="Example: MSFT, COST",
    help="Enter temporary symbols, then save any of them to My Universe if desired.",
)
st.session_state["custom_symbols"] = custom

custom_symbols = list(
    dict.fromkeys(
        s.strip().upper()
        for s in custom.split(",")
        if s.strip()
    )
)

# If a detail view is already open, a CHANGED single-symbol search implies
# that the user wants the existing detail area updated to that symbol.
# The search must have changed since the previous rerun; otherwise a later
# ticker-button click must remain authoritative and must not be overwritten
# by the unchanged search field.
current_search_key = ",".join(custom_symbols)
previous_search_key = st.session_state.get("_previous_custom_search_key", "")

if (
    st.session_state.get("show_details_for")
    and len(custom_symbols) == 1
    and current_search_key != previous_search_key
):
    st.session_state["show_details_for"] = custom_symbols[0]

st.session_state["_previous_custom_search_key"] = current_search_key

custom_analyses: Dict[str, Dict] = {}
if custom_symbols:
    st.markdown("#### Custom symbols")
    for symbol in sorted(custom_symbols):
        try:
            custom_analyses[symbol] = analyze_symbol(symbol, context)
        except Exception as exc:
            st.warning(f"{symbol}: {exc}")

    render_symbol_buttons(custom_analyses, key_prefix="custom")

    for symbol in sorted(custom_analyses):
        if symbol not in current_universe:
            if st.button(
                f"Save {symbol} to My Universe",
                key=f"save_{symbol}",
            ):
                try:
                    save_symbol_for_user(user_email, symbol)
                    st.success(f"{symbol} was saved to My Universe.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not save {symbol}.")
                    st.caption(str(exc))

if st.button("🔄 Refresh results", type="primary"):
    st.cache_data.clear()
    st.session_state.pop("show_details_for", None)
    st.rerun()

if errors:
    with st.expander("Symbols with data errors", expanded=False):
        for symbol, message in errors.items():
            st.write(f"**{symbol}:** {message}")

selected = st.session_state.get("show_details_for")

if selected:
    try:
        detail = analyses.get(selected) or custom_analyses.get(selected)
        if detail is None:
            detail = analyze_symbol(selected, context)

        render_combined(detail, context)

        st.markdown("### Details")
        render_market_section(context)
        render_qqq_section(context)
        render_stock_section(detail)

    except Exception as exc:
        st.error(f"Could not analyze {selected}: {exc}")

st.markdown("---")
st.markdown("Questions? mojconsulting@gmail.com")
