from __future__ import annotations

import json
import unicodedata
import re
import requests
import stripe
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import traffic_lights_core as tl


# =============================================================================
# App configuration
# =============================================================================

st.set_page_config(
    page_title="Smart Market",
    page_icon="🚦",
    layout="wide",
)


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
        st.title("🚦 Smart Market")
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
    """Headers for Supabase REST requests.

    New Supabase sb_secret_ keys are sent via apikey. Legacy JWT service-role
    keys also receive the Authorization bearer header for compatibility.
    """
    key = str(_secret_value("supabase", "service_role_key")).strip()
    headers = {
        "apikey": key,
        "Content-Type": "application/json",
    }
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def supabase_url(path: str) -> str:
    base = str(_secret_value("supabase", "url")).rstrip("/")
    return f"{base}/rest/v1/{path.lstrip('/')}"


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None



def _normalize_signup_name(name: str) -> str:
    """Normalize a Google profile name for duplicate-name review checks."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _alert_secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets["alerts"][name]
        return str(value or "").strip()
    except Exception:
        return default


def _send_signup_review_email(email: str, name: str, reasons: List[str]) -> tuple[bool, str]:
    """Send a review alert through Resend. Failure never blocks signup state persistence."""
    api_key = _alert_secret("resend_api_key")
    admin_email = _alert_secret("admin_email")
    from_email = _alert_secret("from_email", "Smart Market <onboarding@resend.dev>")

    if not api_key or not admin_email:
        return False, "Email alerts are not configured."

    reason_lines = "\n".join(f"- {reason}" for reason in reasons)
    subject = "Smart Market signup requires review"
    body = (
        "A new Smart Market signup was flagged for manual review.\n\n"
        f"Email: {email}\n"
        f"Name: {name or '(not provided)'}\n"
        "Reason(s):\n"
        f"{reason_lines}\n\n"
        "Open Supabase -> app_users and review this account. "
        "If acceptable, change status from pending to approved."
    )

    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_email,
                "to": [admin_email],
                "subject": subject,
                "text": body,
            },
            timeout=15,
        )
        r.raise_for_status()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _signup_risk_assessment(email: str, normalized_name: str) -> tuple[bool, List[str]]:
    """Return whether a new signup needs manual review and the review reasons.

    Current automated review signals:
    - same normalized Google profile name as an existing account;
    - unusually dense signup burst (5+ prior signup requests in 10 minutes).

    Same email is handled separately by the email primary key and can never create
    a second user/trial row.
    """
    reasons: List[str] = []

    if normalized_name:
        r = requests.get(
            supabase_url("app_users"),
            headers=supabase_headers(),
            params={
                "select": "email",
                "normalized_name": f"eq.{normalized_name}",
                "email": f"neq.{email}",
                "limit": "1",
            },
            timeout=15,
        )
        r.raise_for_status()
        if r.json():
            reasons.append("Duplicate normalized profile name")

    # High-threshold velocity signal. This is intentionally not a low threshold
    # because a legitimate publicity spike can create several signups at once.
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    r = requests.get(
        supabase_url("app_users"),
        headers=supabase_headers(),
        params={
            "select": "email,requested_at",
            "requested_at": f"gte.{cutoff}",
            "limit": "10",
        },
        timeout=15,
    )
    r.raise_for_status()
    recent = r.json()
    if len(recent) >= 5:
        reasons.append("Unusually high signup volume in the last 10 minutes")

    return bool(reasons), reasons


def _mark_review_notification(email: str, sent: bool, error: str = "") -> None:
    payload = {
        "review_notified_at": datetime.now(timezone.utc).isoformat() if sent else None,
        "review_notification_error": None if sent else (error or None),
    }
    try:
        requests.patch(
            supabase_url("app_users"),
            headers=supabase_headers(),
            params={"email": f"eq.{email}"},
            json=payload,
            timeout=15,
        )
    except Exception:
        pass


def get_user_access_record(email: str) -> Dict:
    """Return the user's access record, creating and risk-classifying it if needed."""
    params = {
        "email": f"eq.{email}",
        "select": (
            "email,name,status,approved_at,trial_started_at,trial_ends_at,"
            "subscription_status,stripe_customer_id,stripe_subscription_id,"
            "subscription_current_period_end,subscription_cancel_at_period_end,"
            "stripe_payment_method_id,card_fingerprint,card_brand,card_last4,"
            "card_fingerprint_reused,trial_eligibility_status,"
            "review_required,review_reason,normalized_name,review_notified_at,"
            "review_notification_error,requested_at"
        ),
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
        # Same Google email always resolves to the same database row, so it can
        # never obtain a second trial merely by signing in again.
        return rows[0]

    name = str(getattr(st.user, "name", "") or "").strip()
    normalized_name = _normalize_signup_name(name)
    review_required, reasons = _signup_risk_assessment(email, normalized_name)

    # Low-risk users are approved automatically. Flagged users remain pending
    # until the administrator manually reviews them.
    status = "pending" if review_required else "approved"
    payload = {
        "email": email,
        "name": name,
        "normalized_name": normalized_name or None,
        "status": status,
        "review_required": review_required,
        "review_reason": "; ".join(reasons) if reasons else None,
    }

    r = requests.post(
        supabase_url("app_users"),
        headers={**supabase_headers(), "Prefer": "return=representation"},
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    created = r.json()
    record = created[0] if created else payload

    if review_required:
        sent, error = _send_signup_review_email(email, name, reasons)
        _mark_review_notification(email, sent, error)
        record["review_notified_at"] = (
            datetime.now(timezone.utc).isoformat() if sent else None
        )
        record["review_notification_error"] = None if sent else error

    return record


def _initialize_trial_if_missing(record: Dict) -> Dict:
    """Compatibility helper.

    New approved users do NOT receive trial dates until Stripe has collected a
    payment method and the card fingerprint has passed the one-trial check.
    Existing users keep any trial dates already stored in Supabase.
    """
    return record


def _stripe_secret_key() -> str:
    return str(_secret_value("stripe", "secret_key")).strip()


def _stripe_price_id() -> str:
    return str(_secret_value("stripe", "price_id")).strip()


def _app_base_url() -> str:
    try:
        configured = str(st.secrets["stripe"].get("app_url", "") or "").strip().rstrip("/")
    except Exception:
        configured = ""
    return configured or "https://mojmarket.streamlit.app"


def _stripe_subscription_fields(subscription) -> Dict:
    status = str(getattr(subscription, "status", "") or "inactive").lower()
    period_end = _stripe_subscription_period_end(subscription)
    return {
        "subscription_status": status,
        "stripe_subscription_id": str(getattr(subscription, "id", "") or ""),
        "stripe_customer_id": str(getattr(subscription, "customer", "") or ""),
        "subscription_current_period_end": period_end.isoformat() if period_end else None,
        "subscription_cancel_at_period_end": bool(getattr(subscription, "cancel_at_period_end", False)),
    }


def _update_subscription_record(email: str, fields: Dict) -> Dict:
    r = requests.patch(
        supabase_url("app_users"),
        headers={**supabase_headers(), "Prefer": "return=representation"},
        params={"email": f"eq.{email}"},
        json=fields,
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else fields



def _find_reused_card_fingerprint(email: str, fingerprint: str) -> bool:
    """Return True when another user already received a trial with this card fingerprint."""
    if not fingerprint:
        return False
    r = requests.get(
        supabase_url("app_users"),
        headers=supabase_headers(),
        params={
            "select": "email",
            "card_fingerprint": f"eq.{fingerprint}",
            "email": f"neq.{email}",
            "trial_started_at": "not.is.null",
            "limit": "1",
        },
        timeout=15,
    )
    r.raise_for_status()
    return bool(r.json())


def _stripe_payment_method_fields(customer_id: str, email: str) -> Dict:
    """Capture Stripe card metadata without ever handling raw card numbers."""
    if not customer_id:
        return {}

    stripe.api_key = _stripe_secret_key()
    payment_method = None

    # Prefer the Customer's configured default payment method.
    try:
        customer = stripe.Customer.retrieve(customer_id)
        invoice_settings = getattr(customer, "invoice_settings", None)
        default_pm = getattr(invoice_settings, "default_payment_method", None) if invoice_settings else None
        if default_pm:
            payment_method = stripe.PaymentMethod.retrieve(str(default_pm))
    except Exception:
        payment_method = None

    # Subscription-mode Checkout saves the payment method to the Customer.
    # If no explicit default is configured, use the most recently attached card.
    if payment_method is None:
        methods = stripe.PaymentMethod.list(
            customer=customer_id,
            type="card",
            limit=10,
        )
        data = list(getattr(methods, "data", []) or [])
        if data:
            data.sort(key=lambda pm: int(getattr(pm, "created", 0) or 0), reverse=True)
            payment_method = data[0]

    if payment_method is None:
        return {}

    card = getattr(payment_method, "card", None)
    if card is None:
        return {}

    fingerprint = str(getattr(card, "fingerprint", "") or "").strip()
    fields = {
        "stripe_payment_method_id": str(getattr(payment_method, "id", "") or ""),
        "card_fingerprint": fingerprint or None,
        "card_brand": str(getattr(card, "brand", "") or "") or None,
        "card_last4": str(getattr(card, "last4", "") or "") or None,
        "card_fingerprint_reused": _find_reused_card_fingerprint(email, fingerprint)
        if fingerprint
        else False,
    }
    return fields



def create_trial_payment_method_session(email: str, record: Dict):
    """Collect a payment method without charging the customer."""
    stripe.api_key = _stripe_secret_key()
    base = _app_base_url()

    kwargs = {
        "mode": "setup",
        "client_reference_id": email,
        "success_url": f"{base}/?trial_setup=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/?trial_setup=cancelled",
        "payment_method_types": ["card"],
        "metadata": {"user_email": email, "purpose": "trial_eligibility"},
    }

    customer_id = str(record.get("stripe_customer_id") or "").strip()
    if customer_id:
        kwargs["customer"] = customer_id
    else:
        kwargs["customer_email"] = email

    return stripe.checkout.Session.create(**kwargs)


def _payment_method_fields_from_id(payment_method_id: str, email: str) -> Dict:
    """Read non-sensitive Stripe card metadata from a PaymentMethod ID."""
    if not payment_method_id:
        return {}

    stripe.api_key = _stripe_secret_key()
    payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
    card = getattr(payment_method, "card", None)
    if card is None:
        raise RuntimeError("The selected payment method is not a supported card.")

    fingerprint = str(getattr(card, "fingerprint", "") or "").strip()
    return {
        "stripe_payment_method_id": str(getattr(payment_method, "id", "") or ""),
        "card_fingerprint": fingerprint or None,
        "card_brand": str(getattr(card, "brand", "") or "") or None,
        "card_last4": str(getattr(card, "last4", "") or "") or None,
        "card_fingerprint_reused": _find_reused_card_fingerprint(email, fingerprint)
        if fingerprint
        else False,
    }


def confirm_trial_payment_method_return(email: str) -> None:
    """Finalize the pre-trial card check after Stripe Checkout returns."""
    params = st.query_params
    if params.get("trial_setup") != "success":
        return

    session_id = params.get("session_id")
    if not session_id:
        return

    try:
        stripe.api_key = _stripe_secret_key()
        session = stripe.checkout.Session.retrieve(
            session_id,
            expand=["setup_intent.payment_method"],
        )

        session_email = str(
            getattr(session, "client_reference_id", "") or ""
        ).strip().lower()
        if session_email != email:
            raise RuntimeError("Trial setup session does not belong to the signed-in user.")

        customer_id = str(getattr(session, "customer", "") or "")
        setup_intent = getattr(session, "setup_intent", None)
        payment_method = getattr(setup_intent, "payment_method", None) if setup_intent else None
        payment_method_id = str(getattr(payment_method, "id", payment_method) or "")
        if not payment_method_id:
            raise RuntimeError("Stripe did not return a payment method.")

        fields = _payment_method_fields_from_id(payment_method_id, email)
        fields["stripe_customer_id"] = customer_id or None

        reused = bool(fields.get("card_fingerprint_reused"))
        if reused:
            # The payment method is valid, but this card has already received a
            # Smart Market free trial on another account. Do not grant another trial.
            fields.update(
                {
                    "trial_started_at": None,
                    "trial_ends_at": None,
                    "trial_eligibility_status": "reused_card",
                }
            )
            _update_subscription_record(email, fields)
            st.session_state.pop("_trial_setup_checkout_url", None)
            st.session_state.pop("_trial_setup_checkout_email", None)
            st.query_params.clear()
            st.warning(
                "This payment method has already been used for a Smart Market free trial. "
                "You can continue with a paid subscription."
            )
            st.rerun()

        from datetime import timedelta
        now = datetime.now(timezone.utc)
        trial_end = now + timedelta(days=30)
        fields.update(
            {
                "trial_started_at": now.isoformat(),
                "trial_ends_at": trial_end.isoformat(),
                "trial_eligibility_status": "eligible",
            }
        )
        _update_subscription_record(email, fields)
        st.session_state.pop("_trial_setup_checkout_url", None)
        st.session_state.pop("_trial_setup_checkout_email", None)
        st.query_params.clear()
        st.success(
            f"Your 30-day free trial is active through {trial_end.strftime('%b %d, %Y')}. "
            "Your card will not be charged during the free trial."
        )
        st.rerun()

    except Exception as exc:
        st.error("We could not verify your payment method for the free trial.")
        st.caption(str(exc))


def render_trial_payment_method_screen(email: str, record: Dict) -> None:
    """Require payment-method verification before starting a new free trial."""
    st.title("🚦 Smart Market")
    st.markdown("## Start your 30-day free trial")
    st.write(
        "Verify a payment method to confirm you are a real user. "
        "You will not be charged during the 30-day free trial."
    )
    st.caption(
        "Payment details are handled securely by Stripe."
    )

    cached_email = st.session_state.get("_trial_setup_checkout_email")
    checkout_url = (
        st.session_state.get("_trial_setup_checkout_url")
        if cached_email == email
        else None
    )
    checkout_error = None

    if not checkout_url:
        try:
            session = create_trial_payment_method_session(email, record)
            checkout_url = session.url
            st.session_state["_trial_setup_checkout_url"] = checkout_url
            st.session_state["_trial_setup_checkout_email"] = email
        except Exception as exc:
            checkout_error = str(exc)

    if checkout_url:
        st.link_button(
            "Verify payment method",
            checkout_url,
            type="primary",
            use_container_width=False,
        )
    else:
        st.error("Could not open secure payment-method verification.")
        if checkout_error:
            st.caption(checkout_error)

    st.caption(f"Signed in as {email}")
    if st.button("Sign out", key="trial_setup_signout"):
        st.logout()
    st.stop()


def create_checkout_session(email: str, record: Dict):
    """Create a Stripe Checkout Session, reusing the customer's Stripe ID when available."""
    stripe.api_key = _stripe_secret_key()
    base = _app_base_url()

    kwargs = {
        "mode": "subscription",
        "line_items": [{"price": _stripe_price_id(), "quantity": 1}],
        "client_reference_id": email,
        "success_url": f"{base}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/?checkout=cancelled",
        "allow_promotion_codes": False,
        "subscription_data": {"metadata": {"user_email": email}},
        "metadata": {"user_email": email},
    }

    customer_id = str(record.get("stripe_customer_id") or "").strip()
    if customer_id:
        kwargs["customer"] = customer_id
    else:
        kwargs["customer_email"] = email

    return stripe.checkout.Session.create(**kwargs)


def confirm_checkout_return(email: str) -> None:
    params = st.query_params
    if params.get("checkout") != "success":
        return

    session_id = params.get("session_id")
    if not session_id:
        return


    try:
        stripe.api_key = _stripe_secret_key()
        session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
        session_email = str(getattr(session, "client_reference_id", "") or "").strip().lower()
        if session_email != email:
            raise RuntimeError("Checkout session does not belong to the signed-in user.")

        subscription = getattr(session, "subscription", None)
        if not subscription:
            raise RuntimeError("Stripe did not return a subscription for this checkout session.")

        fields = _stripe_subscription_fields(subscription)
        customer_id = str(fields.get("stripe_customer_id") or "")
        fields.update(_stripe_payment_method_fields(customer_id, email))
        _update_subscription_record(email, fields)
        st.session_state.pop("_stripe_checkout_url", None)
        st.session_state.pop("_stripe_checkout_email", None)
        st.query_params.clear()
        st.success("Subscription activated. Welcome to Smart Market.")
        st.rerun()
    except Exception as exc:
        st.error("Your payment was received, but subscription verification could not be completed.")
        st.caption(str(exc))



def _stripe_subscription_period_end(subscription) -> Optional[datetime]:
    """Return the authoritative current billing-period end.

    Modern Stripe API versions keep period dates on subscription items. For a
    cancellation scheduled at period end, use Stripe's resolved cancel_at
    timestamp when present. If a returned subscription item is thin and lacks
    current_period_end, retrieve that subscription item directly.
    """
    ts = None

    # When Stripe has resolved the scheduled cancellation timestamp, this is
    # the most direct value for the access-until date.
    try:
        if bool(getattr(subscription, "cancel_at_period_end", False)):
            ts = getattr(subscription, "cancel_at", None)
    except Exception:
        ts = None
    if ts is None and isinstance(subscription, dict):
        if bool(subscription.get("cancel_at_period_end")):
            ts = subscription.get("cancel_at")

    item_id = None
    if ts is None:
        try:
            items = getattr(subscription, "items", None)
            data = getattr(items, "data", None) if items is not None else None
            if data:
                first = data[0]
                ts = getattr(first, "current_period_end", None)
                item_id = getattr(first, "id", None)
                if isinstance(first, dict):
                    ts = ts or first.get("current_period_end")
                    item_id = item_id or first.get("id")
        except Exception:
            ts = None

    # Defensive fallback: explicitly retrieve the single Smart Market
    # subscription item if Stripe did not include its period end inline.
    if ts is None and item_id:
        try:
            item = stripe.SubscriptionItem.retrieve(str(item_id))
            ts = getattr(item, "current_period_end", None)
            if ts is None and isinstance(item, dict):
                ts = item.get("current_period_end")
        except Exception:
            ts = None

    # Legacy fallback for pre-Basil Stripe API versions.
    if ts is None:
        try:
            ts = getattr(subscription, "current_period_end", None)
        except Exception:
            ts = None
    if ts is None and isinstance(subscription, dict):
        ts = subscription.get("current_period_end")

    return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None

def sync_subscription_from_stripe(email: str, record: Dict) -> Dict:
    sub_id = str(record.get("stripe_subscription_id") or "").strip()
    if not sub_id:
        return record
    try:
        stripe.api_key = _stripe_secret_key()
        subscription = stripe.Subscription.retrieve(sub_id)
        fields = _stripe_subscription_fields(subscription)
        updated = _update_subscription_record(email, fields)
        return {**record, **updated}
    except Exception:
        return record




def cancel_subscription_at_period_end(email: str, record: Dict) -> Dict:
    """Schedule the current Stripe subscription to cancel at period end."""
    sub_id = str(record.get("stripe_subscription_id") or "").strip()
    if not sub_id:
        raise RuntimeError("No Stripe subscription is associated with this account.")

    stripe.api_key = _stripe_secret_key()
    subscription = stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
    fields = _stripe_subscription_fields(subscription)
    updated = _update_subscription_record(email, fields)
    return {**record, **updated}


def resume_subscription(email: str, record: Dict) -> Dict:
    """Resume a subscription that is scheduled to cancel at period end."""
    sub_id = str(record.get("stripe_subscription_id") or "").strip()
    if not sub_id:
        raise RuntimeError("No Stripe subscription is associated with this account.")

    stripe.api_key = _stripe_secret_key()
    subscription = stripe.Subscription.modify(sub_id, cancel_at_period_end=False)
    fields = _stripe_subscription_fields(subscription)
    updated = _update_subscription_record(email, fields)
    return {**record, **updated}


def create_customer_portal_session(record: Dict):
    """Create a Stripe-hosted Customer Portal session for billing recovery."""
    customer_id = str(record.get("stripe_customer_id") or "").strip()
    if not customer_id:
        raise RuntimeError("No Stripe customer is associated with this account.")

    stripe.api_key = _stripe_secret_key()
    return stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=_app_base_url(),
    )


def render_payment_recovery_screen(email: str, record: Dict) -> None:
    """Block dashboard access while a subscription has a payment problem."""
    st.title("🚦 Smart Market")
    st.markdown("## Payment issue")
    st.warning(
        "We could not successfully renew your Smart Market subscription. "
        "Please update your payment method to restore full access."
    )

    portal_error = None
    portal_url = None
    try:
        portal = create_customer_portal_session(record)
        portal_url = portal.url
    except Exception as exc:
        portal_error = str(exc)

    if portal_url:
        st.link_button(
            "Update payment method",
            portal_url,
            type="primary",
            use_container_width=False,
        )
    else:
        st.error("Could not open Stripe billing management.")
        if portal_error:
            st.caption(portal_error)

    if st.button("Check payment status"):
        refreshed = sync_subscription_from_stripe(email, record)
        refreshed_status = str(refreshed.get("subscription_status") or "inactive").lower()
        if refreshed_status in {"active", "trialing"}:
            st.success("Payment status restored.")
        st.rerun()

    st.caption(
        "Your account remains signed in, but the market dashboard is unavailable "
        "until the subscription payment issue is resolved."
    )
    st.caption(f"Signed in as {email}")
    if st.button("Sign out", key="payment_issue_signout"):
        st.logout()
    st.stop()


def render_subscription_screen(email: str, record: Dict) -> None:
    st.title("🚦 Smart Market")
    st.markdown("## Continue your access")
    if str(record.get("trial_eligibility_status") or "") == "reused_card":
        st.write(
            "This payment method has already received a Smart Market free trial. "
            "A new free trial is not available for this account."
        )
    else:
        st.write("Your 30-day introductory access period has ended.")

    st.markdown(
        """
        <div style="max-width:620px;padding:1.5rem 1.6rem;border:1px solid #444;"
        "border-radius:1rem;margin:1rem 0 1.25rem 0;">
          <div style="font-size:1.1rem;font-weight:700;">MOJ Market Subscription</div>
          <div style="font-size:2rem;font-weight:800;margin-top:.35rem;">$49.99/month</div>
          <div style="margin-top:.6rem;opacity:.88;">Full access to the market and stock traffic-light dashboard and your saved universe.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Keep the primary subscription CTA compact on desktop while remaining
    # full-width on narrow/mobile screens.
    st.markdown(
        """
        <style>
        div[data-testid="stButton"] button[kind="primary"] {
            width: 100% !important;
            max-width: 620px !important;
        }
        div[data-testid="stLinkButton"] a {
            width: 100% !important;
            max-width: 620px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Prepare one Stripe Checkout Session before rendering the CTA so the
    # subscription button is a direct link to Stripe (no intermediate
    # Streamlit rerun/redirect page). Reuse it for the current browser
    # session to avoid creating a new Checkout Session on every rerun.
    cached_email = st.session_state.get("_stripe_checkout_email")
    checkout_url = (
        st.session_state.get("_stripe_checkout_url")
        if cached_email == email
        else None
    )
    checkout_error = None
    if not checkout_url:
        try:
            session = create_checkout_session(email, record)
            checkout_url = session.url
            st.session_state["_stripe_checkout_url"] = checkout_url
            st.session_state["_stripe_checkout_email"] = email
        except Exception as exc:
            checkout_error = str(exc)

    if checkout_url:
        st.link_button(
            "Subscribe",
            checkout_url,
            type="primary",
            use_container_width=True,
        )
    else:
        st.error("Could not start Stripe checkout.")
        if checkout_error:
            st.caption(checkout_error)

    st.caption("Secure checkout is handled by Stripe. Card, Apple Pay, and Google Pay may appear when supported by the user's device and browser.")

    st.caption(f"Signed in as {email}")
    if st.button("Sign out"):
        st.logout()
    st.stop()


def access_gate(email: str) -> Dict:
    """Enforce approval, card-gated trial eligibility, and paid subscription access."""
    try:
        record = get_user_access_record(email)
        record = _initialize_trial_if_missing(record)
    except Exception as exc:
        st.error("The user-access service is unavailable.")
        st.caption(str(exc))
        st.stop()

    status = str(record.get("status", "pending")).lower()

    if status == "denied":
        st.title("🚦 Smart Market")
        st.error("Access to this app has not been approved for this account.")
        if st.button("Sign out"):
            st.logout()
        st.stop()

    if status != "approved":
        st.title("🚦 Smart Market")
        if bool(record.get("review_required")):
            st.warning(
                "Your account requires a brief administrator review before access can continue."
            )
            st.caption(
                "You do not need to create another account. You will be able to continue "
                "with this same Google account after approval."
            )
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

    # Stripe is authoritative whenever a paid subscription already exists.
    record = sync_subscription_from_stripe(email, record)
    subscription_status = str(record.get("subscription_status") or "inactive").lower()

    if subscription_status in {"active", "trialing"}:
        return record

    if subscription_status in {"past_due", "unpaid", "incomplete", "paused"}:
        render_payment_recovery_screen(email, record)

    trial_ends_at = _parse_ts(record.get("trial_ends_at"))
    now = datetime.now(timezone.utc)

    # Existing valid trial remains accessible.
    if trial_ends_at and now < trial_ends_at:
        return record

    # A reused card is never granted another introductory trial.
    if str(record.get("trial_eligibility_status") or "") == "reused_card":
        render_subscription_screen(email, record)

    # Approved users who have never started a trial must verify a payment
    # method first. Stripe collects it without charging the card.
    if not record.get("trial_started_at"):
        render_trial_payment_method_screen(email, record)

    # Trial existed but has now ended.
    render_subscription_screen(email, record)
    return record


def render_trial_status(record: Dict, email: str) -> None:
    subscription_status = str(record.get("subscription_status") or "inactive").lower()
    cancel_at_period_end = bool(record.get("subscription_cancel_at_period_end", False))
    period_end = _parse_ts(record.get("subscription_current_period_end"))

    if subscription_status in {"active", "trialing"}:
        if cancel_at_period_end:
            if period_end:
                st.success(f"Subscription active until {period_end.strftime('%b %d, %Y')}")
            else:
                st.success("Subscription active until the end of the current billing period")

            if st.button("Resume subscription", key="resume_subscription_sidebar"):
                try:
                    resume_subscription(email, record)
                    st.success("Subscription resumed.")
                    st.rerun()
                except Exception as exc:
                    st.error("Could not resume the subscription.")
                    st.caption(str(exc))
            return

        st.success("Subscription active")
        sub_id = str(record.get("stripe_subscription_id") or "").strip()
        if sub_id:
            confirm_key = "_confirm_subscription_cancel"
            if not st.session_state.get(confirm_key, False):
                if st.button("Cancel subscription", key="cancel_subscription_sidebar"):
                    st.session_state[confirm_key] = True
                    st.rerun()
            else:
                if period_end:
                    st.warning(
                        f"Cancel at the end of the current billing period on {period_end.strftime('%b %d, %Y')}?"
                    )
                    st.caption(
                        f"You will continue to have full access until {period_end.strftime('%b %d, %Y')}."
                    )
                else:
                    st.warning("Cancel at the end of the current billing period?")
                    st.caption("You will continue to have full access through the current billing period.")

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Confirm cancellation", key="confirm_cancel_subscription"):
                        try:
                            updated = cancel_subscription_at_period_end(email, record)
                            st.session_state[confirm_key] = False
                            updated_period_end = _parse_ts(updated.get("subscription_current_period_end"))
                            if updated_period_end:
                                st.success(
                                    f"Cancellation scheduled. Access continues until {updated_period_end.strftime('%b %d, %Y')}."
                                )
                            else:
                                st.success("Cancellation scheduled for the end of the current billing period.")
                            st.rerun()
                        except Exception as exc:
                            st.error("Could not cancel the subscription.")
                            st.caption(str(exc))
                with col2:
                    if st.button("Keep Subscription - $49.99/month", key="keep_subscription"):
                        st.session_state[confirm_key] = False
                        st.rerun()
        return

    trial_end = _parse_ts(record.get("trial_ends_at"))
    if not trial_end:
        return
    now = datetime.now(timezone.utc)
    seconds = max(0, int((trial_end - now).total_seconds()))
    days = (seconds + 86399) // 86400
    if days > 0:
        st.caption(f"Introductory access: {days} day{'s' if days != 1 else ''} remaining")

def render_membership_status(record: Dict) -> None:
    """Render compact membership status for the main page header."""
    subscription_status = str(record.get("subscription_status") or "inactive").lower()
    cancel_at_period_end = bool(record.get("subscription_cancel_at_period_end", False))
    period_end = _parse_ts(record.get("subscription_current_period_end"))

    if subscription_status in {"active", "trialing"}:
        if cancel_at_period_end and period_end:
            label = f"Subscribed member until {period_end.strftime('%b %d, %Y')}"
        else:
            label = "Subscribed member"
    else:
        trial_end = _parse_ts(record.get("trial_ends_at"))
        now = datetime.now(timezone.utc)
        if trial_end and now < trial_end:
            label = f"Free trial until {trial_end.strftime('%b %d, %Y')}"
        else:
            label = "Subscription required"

    st.markdown(
        f"<div style='text-align:right;font-weight:600;padding-top:0.7rem;'>{label}</div>",
        unsafe_allow_html=True,
    )


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
confirm_trial_payment_method_return(user_email)
confirm_checkout_return(user_email)
access_record = access_gate(user_email)

try:
    saved_symbols = load_saved_symbols(user_email)
except Exception as exc:
    st.error("Could not load your saved universe.")
    st.caption(str(exc))
    saved_symbols = set()

current_universe = sorted(set(UNIVERSE) | saved_symbols)

title_col, status_col = st.columns([3, 2])
with title_col:
    st.title("🚦 Smart Market")
with status_col:
    render_membership_status(access_record)

st.caption(
    "Traffic-light output is a decision-support tool, not a guarantee of market direction "
    "or investment outcome."
)

with st.sidebar:
    st.caption(f"Signed in as {user_email}")
    render_trial_status(access_record, user_email)
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
