#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interactive PL/PLN BLIK checkout service.

The service creates the ChatGPT checkout through the existing Sentinel-aware
transport, then keeps one Playwright page alive. Stripe.js owns Elements,
Radar, passive hCaptcha, confirm and approval requests. A six-digit BLIK code
is accepted on stdin only after the page reports that its BLIK field is ready.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent
GOPAY_ROOT = ROOT / "gopay_engine"
GCASH_ROOT = ROOT / "gcash_engine"
for value in (str(GOPAY_ROOT), str(GCASH_ROOT), str(Path(__file__).resolve().parent)):
    if value not in sys.path:
        sys.path.insert(0, value)

from gopay_custom_flow import (  # noqa: E402
    _APPROVE_URL,
    _CHECKOUT_URL,
    _PRICING_CONFIG_URL,
    _STRIPE_VERSION,
    _approval_risk_headers,
    _chatgpt_headers,
    _jwt_account_id,
    _mint_checkout_risk_context,
    _new_session,
    _response_detail,
    _stripe_headers,
)
from gcash_custom_flow import (  # noqa: E402
    _chromium_executable,
    _fingerprint_init_script,
    _prepare_chromium_runtime,
    _proxy_for_playwright,
)
from sms_tool.gopay_browser import (  # noqa: E402
    _add_cookie_header,
    _click_first_ready,
    _click_terms_if_present,
    _fill_billing_details_if_present,
    _iter_frames,
    _launch_playwright_browser,
)
from sms_tool.paypal_proxy import normalize_proxy_url, probe_proxy, redact_proxy_url  # noqa: E402
from pl_billing import generate_pl_billing  # noqa: E402


_SUBSCRIPTIONS_URL = "https://chatgpt.com/backend-api/subscriptions"
_STRIPE_PAGE_URL = "https://api.stripe.com/v1/payment_pages/{checkout_session_id}"
_CHECKOUT_VERIFY_PATH = "/checkout/verify"
# The ChatGPT web client identifies itself with the deployment it was built
# from, and the Sentinel deployment attestation it mints carries the same
# deploy id.  The 2026-09-09 BLIK HAR shows that coupling directly:
# oai-client-version=prod-ee7435ab64168895bed5568867e37317d3926b94, while the
# attestation payload is {"deployId":"ee7435ab64168895bed5568867e37317d3926b94"}.
# A stale client version is therefore internally inconsistent with the
# attestation, so keep this in step with the newest captured HAR; deployments
# can override it through the environment without touching other channels.
_CHATGPT_CLIENT_VERSION = (
    os.environ.get("BLIK_CHATGPT_CLIENT_VERSION")
    or "prod-ee7435ab64168895bed5568867e37317d3926b94"
)
_CHATGPT_CLIENT_BUILD = os.environ.get("BLIK_CHATGPT_CLIENT_BUILD") or "10448717"
_CLIENT_IDENTITY = {
    "oai-client-version": _CHATGPT_CLIENT_VERSION,
    "oai-client-build-number": _CHATGPT_CLIENT_BUILD,
}
_BLIK_SELECTORS = (
    'input#payment-method-accordion-item-title-blik',
    'input[value="blik"][name="payment-method-accordion-item-title"]',
    '#payment-method-label-blik',
    '[role="radio"][aria-label*="blik" i]',
    'button[aria-label*="blik" i]',
    'button:has-text("BLIK")',
    'label:has-text("BLIK")',
    '[data-testid*="blik" i]',
)
_BLIK_CODE_SELECTORS = (
    'input[name="blik-code"]',
    'input[name="blik_code"]',
    'input[name="blikCode"]',
    'input[autocomplete="one-time-code"]',
    'input[aria-label*="Kod BLIK" i]',
    'input[placeholder="123 456"]',
    'input[data-testid*="blik" i]',
    'input[id*="blik" i]',
    'input[inputmode="numeric"]',
)
_SUBMIT_SELECTORS = (
    'button[data-testid="hosted-payment-submit-button"]',
    'button[type="submit"]:has-text("Zasubskrybuj")',
    'button[type="submit"]:has-text("Zapłać")',
    'button:has-text("Subscribe")',
    'button:has-text("Pay")',
    'button:has-text("Zapłać")',
    'button:has-text("Zasubskrybuj")',
    'button:has-text("Subskrybuj")',
)

_PL_DEVICE_PROFILES = (
    {
        "screen": "1920x1080", "device_scale_factor": 1, "hardware_concurrency": 8, "device_memory": 8,
        "webgl_vendor": "Google Inc. (Intel)",
        "gpu_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620, D3D11)",
    },
    {
        "screen": "1920x1080", "device_scale_factor": 1, "hardware_concurrency": 12, "device_memory": 16,
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "gpu_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060, D3D11)",
    },
    {
        "screen": "2560x1440", "device_scale_factor": 1, "hardware_concurrency": 16, "device_memory": 16,
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "gpu_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070, D3D11)",
    },
    {
        "screen": "1536x864", "device_scale_factor": 1.25, "hardware_concurrency": 8, "device_memory": 8,
        "webgl_vendor": "Google Inc. (Intel)",
        "gpu_renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, D3D11)",
    },
    {
        "screen": "1366x768", "device_scale_factor": 1, "hardware_concurrency": 4, "device_memory": 8,
        "webgl_vendor": "Google Inc. (Intel)",
        "gpu_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630, D3D11)",
    },
    {
        "screen": "1920x1080", "device_scale_factor": 1, "hardware_concurrency": 8, "device_memory": 8,
        "webgl_vendor": "Google Inc. (AMD)",
        "gpu_renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics, D3D11)",
    },
)
_PL_WINDOWS_FONTS = (
    "Arial", "Calibri", "Cambria", "Segoe UI", "Times New Roman", "Verdana",
    "Tahoma", "Georgia", "Trebuchet MS", "Courier New",
)


class BlikEligibilityError(RuntimeError):
    """A checkout contract failure that cannot be fixed by retrying the account."""

    def __init__(self, message: str, reason_code: str, **details: Any) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = False
        self.details = details


class BlikPageContractError(RuntimeError):
    """Stripe page controls are missing or incompatible with this flow."""

    def __init__(self, message: str, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = False


def emit(stage: str, status: str, message: str) -> None:
    clean = re.sub(r"\s+", " ", str(message or "")).strip()[:500]
    print(f"[{stage}] [{status}] {clean}", flush=True)


def event(name: str, **payload: Any) -> None:
    print(json.dumps({"event": name, **payload}, ensure_ascii=False, separators=(",", ":")), flush=True)


def _client_identity() -> dict[str, str]:
    """ChatGPT client identity headers for the deployment captured in the HAR."""
    return dict(_CLIENT_IDENTITY)


def _checkout_verify_url(checkout: dict[str, Any]) -> str:
    """Return the ChatGPT return URL Stripe redirects to after bank approval.

    The HAR records the terminal navigation of a successful BLIK purchase:
    /checkout/verify?stripe_session_id=..&processor_entity=..&plan_type=plus.
    """
    query = urlencode(
        {
            "stripe_session_id": str(checkout.get("id") or ""),
            "processor_entity": str(checkout.get("entity") or "openai_ie"),
            "plan_type": "plus",
        }
    )
    return "https://chatgpt.com" + _CHECKOUT_VERIFY_PATH + "?" + query


_SESSION_REFRESH_PATH = "/api/auth/session"


def _refresh_session_request(http: Any, fp: dict[str, Any], cookies: str, timeout: float,
                             query: str, *, referer: str = "https://chatgpt.com/") -> str:
    """GET the cookie-backed ChatGPT session endpoint and return a new token.

    The endpoint authenticates with the browser cookies only - the HAR sends no
    Authorization header - and its response carries ``accessToken`` together
    with the expiry and the account summary.
    """
    path = _SESSION_REFRESH_PATH
    try:
        headers = _chatgpt_headers(
            "",
            path,
            referer=referer,
            device_id=str(fp.get("device_id") or ""),
            session_id=str(fp.get("oai_session_id") or fp.get("session_seed") or ""),
            fingerprint=fp,
            cookie_header=cookies,
            extra=_client_identity(),
        )
        headers.pop("Authorization", None)
        response = http.get(
            "https://chatgpt.com" + path + query,
            headers=headers,
            timeout=min(timeout, 30),
        )
    except Exception:
        return ""
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        return ""
    try:
        payload = response.json() or {}
    except Exception:
        return ""
    token = str(payload.get("accessToken") or "").strip()
    return token if len(token.split(".")) == 3 else ""


def _refresh_session_token(http: Any, fp: dict[str, Any], cookies: str, timeout: float,
                           *, referer: str = "https://chatgpt.com/") -> str:
    """Recover from a mid-run token_expired 401.

    The HAR shows the caller access token expiring mid-checkout: every
    /backend-api call answered 401 {"code":"token_expired"} until the page
    called GET /api/auth/session?refresh=true&reason=token_expired, after which
    the same requests succeeded.  A BLIK run spans several minutes, so the
    engine needs the same recovery.
    """
    return _refresh_session_request(
        http, fp, cookies, timeout,
        "?refresh=true&reason=token_expired&method=GET",
        referer=referer,
    )


def _refresh_workspace_session(http: Any, fp: dict[str, Any], cookies: str, timeout: float,
                               *, referer: str = "https://chatgpt.com/payments/success") -> str:
    """Refresh the session once the checkout reports success (HAR entry 248).

    After the Stripe redirect lands on /payments/success the client calls
    /api/auth/session?workspace_update=true&reason=checkout_success&path=%2Fpayments%2Fsuccess
    and the following subscriptions read already reports the new plan.  The
    engine replays it before polling for Plus so the caller token reflects the
    workspace that was just granted.
    """
    return _refresh_session_request(
        http, fp, cookies, timeout,
        "?workspace_update=true&reason=checkout_success&path=" + quote("/payments/success", safe=""),
        referer=referer,
    )


class _ChatGptSession:
    """Mutable caller token, refreshable from the cookie session mid-run."""

    def __init__(self, access_token: str) -> None:
        self.access_token = str(access_token or "")
        self.refreshed = False

    def refresh(self, http: Any, fp: dict[str, Any], cookies: str, timeout: float,
                *, referer: str = "https://chatgpt.com/") -> bool:
        fresh = _refresh_session_token(http, fp, cookies, timeout, referer=referer)
        if not fresh or fresh == self.access_token:
            return False
        self.access_token = fresh
        self.refreshed = True
        emit("session_refresh", "ok", "会话 Access Token 已刷新（HAR: reason=token_expired）")
        return True


def _polish_fingerprint(raw: str, email: str) -> dict[str, Any]:
    try:
        fp = json.loads(raw) if raw else {}
    except Exception:
        fp = {}
    if not isinstance(fp, dict):
        fp = {}
    fp.update({
        "country": "PL",
        "channel": "blik",
        "locale": "pl-PL",
        "oai_language": "pl-PL",
        "accept_language": "pl-PL,pl;q=0.9,en;q=0.8",
        "timezone": "Europe/Warsaw",
        "account_email": email,
    })
    fp.setdefault("device_id", str(uuid.uuid4()))
    fp.setdefault("session_seed", str(uuid.uuid4()))
    fp.setdefault("oai_session_id", fp["session_seed"])
    profile_seed = hashlib.sha256(str(fp["device_id"]).encode("utf-8")).digest()[0]
    profile = _PL_DEVICE_PROFILES[profile_seed % len(_PL_DEVICE_PROFILES)]
    fp.update(profile)
    fp.update({
        "platform": "Win32",
        "platform_os": "Windows",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
        "color_depth": 24,
        "max_touch_points": 0,
        "fonts": list(_PL_WINDOWS_FONTS),
    })
    return fp


def _cookie_header(session: Any, device_id: str) -> str:
    parts: list[str] = []
    try:
        jar = session.cookies() if callable(getattr(session, "cookies", None)) else session.cookies
        for item in jar:
            domain = str(getattr(item, "domain", "") or "")
            if domain.endswith("chatgpt.com"):
                parts.append(f"{item.name}={item.value}")
    except Exception:
        pass
    if not any(part.startswith("oai-did=") for part in parts):
        parts.append("oai-did=" + device_id)
    return "; ".join(parts)


def _verify_polish_exit(proxy: str) -> None:
    try:
        observed = str(getattr(probe_proxy(proxy, timeout=8), "country_code", "") or "").upper()
    except Exception:
        observed = ""
    if observed and observed != "PL":
        raise RuntimeError(f"BLIK requires a Polish proxy exit; observed {observed}")
    emit("proxy", "ok" if observed == "PL" else "warn", "代理出口=" + (observed or "未探测到；继续使用 PL 强制上下文"))


def _stripe_session_id(data: dict[str, Any]) -> str:
    """Resolve the Stripe Checkout Session id from a create-checkout response.

    The HAR creation response carries the Stripe id in ``checkout_session_id``,
    so that field wins.  An account that cannot use the free trial can instead
    receive a ChatGPT-internal ``oaics_`` session; when such a session still
    exposes a Stripe ``client_secret`` (``cs_..._secret_...``) the Stripe id is
    recovered from it rather than failing the whole run.
    """
    checkout_id = str(data.get("checkout_session_id") or "").strip()
    if checkout_id.startswith("cs_"):
        return checkout_id
    derived = str(data.get("client_secret") or "").strip().split("_secret", 1)[0].strip()
    return derived if derived.startswith("cs_") else checkout_id


def _checkout_diagnostic(data: dict[str, Any]) -> str:
    """Sensitive-free summary of a create-checkout response that cannot be used."""
    fields = {
        "tag": data.get("tag"),
        "checkout_session_id": data.get("checkout_session_id"),
        "checkout_provider": data.get("checkout_provider"),
        "status": data.get("status"),
        "promo_campaign": data.get("promo_campaign"),
    }
    present = {key: value for key, value in fields.items() if value not in (None, "")}
    return json.dumps(present, ensure_ascii=False, separators=(",", ":"))


def _create_checkout(access_token: str, proxy: str, fp: dict[str, Any], promo: str, timeout: float) -> tuple[Any, dict[str, Any], str]:
    device_id = str(fp["device_id"])
    session_id = str(fp.get("oai_session_id") or fp["session_seed"])
    session = _new_session(proxy, fingerprint=fp)
    pricing_path = "/backend-api/checkout_pricing_config/configs/PL"
    emit("pricing_config", "run", "读取 PL/PLN 定价配置")
    pricing = session.get(_PRICING_CONFIG_URL + "/PL", headers=_chatgpt_headers(
        access_token, pricing_path, device_id=device_id, session_id=session_id, fingerprint=fp,
        extra=_client_identity(),
    ), timeout=timeout)
    if pricing.status_code >= 400:
        raise RuntimeError("PL pricing rejected HTTP " + str(pricing.status_code))
    emit("pricing_config", "ok", "PL/PLN 定价配置已读取")

    promo_url = "https://chatgpt.com/?promo_campaign=" + promo if promo else "https://chatgpt.com/"
    try:
        session.get(promo_url, headers=_chatgpt_headers(
            access_token, "", referer="https://chatgpt.com/", device_id=device_id,
            session_id=session_id, fingerprint=fp, extra=_client_identity(),
        ), timeout=timeout)
    except Exception:
        pass
    cookies = _cookie_header(session, device_id)
    risk = _mint_checkout_risk_context(
        session.get, proxy, device_id, session_id, fp, promo, cookies, timeout, emit
    )
    if not risk.get("openai-sentinel-token"):
        raise RuntimeError("BLIK checkout Sentinel token was not generated")
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "PL", "currency": "PLN"},
        "checkout_ui_mode": "custom",
    }
    if promo:
        body["promo_campaign"] = {"promo_campaign_id": promo, "is_coupon_from_query_param": False}
    emit("checkout", "run", "创建 PL/PLN Custom Checkout")
    response = session.post(_CHECKOUT_URL, json=body, headers=_chatgpt_headers(
        access_token, "/backend-api/payments/checkout", referer=promo_url,
        device_id=device_id, session_id=session_id, fingerprint=fp,
        cookie_header=cookies, extra={**risk, **_client_identity()},
    ), timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError("checkout rejected HTTP " + str(response.status_code) + ": " + _response_detail(response))
    data = response.json() or {}
    cs = _stripe_session_id(data)
    entity = str(data.get("processor_entity") or "openai_ie")
    if not cs.startswith("cs_"):
        raise RuntimeError(
            "BLIK requires a Stripe cs_ Custom Checkout session; upstream returned "
            + _checkout_diagnostic(data)
        )
    emit("checkout", "ok", "PL Checkout 已创建，准备加载 BLIK Elements")
    return session, {
        "id": cs,
        "entity": entity,
        "url": f"https://chatgpt.com/checkout/{entity}/{cs}",
        "publishable_key": str(data.get("publishable_key") or ""),
        # Echoed by the creation response; used to explain a non-zero due later.
        "promo_echo": data.get("promo_campaign"),
        # Keep this private to the engine.  The deployment attestation belongs
        # to the same browser/device checkout context and may still be current
        # when the later approval request is made.
        "_checkout_risk": {
            key: str(risk.get(key) or "")
            for key in ("oai-web-deployment-attestation",)
            if risk.get(key)
        },
    }, cookies


def _minor_amount(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _payment_method_types(payload: dict[str, Any]) -> list[str]:
    # The explicit availability list is authoritative. The ordered list is
    # only a compatibility fallback when Stripe omits payment_method_types.
    for key in ("payment_method_types", "ordered_payment_method_types"):
        values = payload.get(key)
        if not isinstance(values, list) or not values:
            continue
        methods: list[str] = []
        for value in values:
            normalized = str(value or "").strip().lower()
            if normalized and normalized not in methods:
                methods.append(normalized)
        if methods:
            return methods
    return []


def _trial_discount(total: dict[str, Any]) -> dict[str, Any] | None:
    discounts = total.get("total_discount_amounts")
    if not isinstance(discounts, list):
        return None
    for discount in discounts:
        if not isinstance(discount, dict):
            continue
        coupon = discount.get("coupon")
        if not isinstance(coupon, dict):
            continue
        name = str(coupon.get("name") or "").strip()
        normalized_name = name.lower()
        percent_off = coupon.get("percent_off")
        duration_months = _minor_amount(coupon.get("duration_in_months"))
        intervals = _minor_amount(discount.get("intervals"))
        is_named_trial = "free trial" in normalized_name and (
            "1 month" in normalized_name or "one month" in normalized_name
        )
        if (
            isinstance(percent_off, (int, float))
            and not isinstance(percent_off, bool)
            and percent_off >= 100
            and is_named_trial
            and (duration_months == 1 or intervals == 1)
        ):
            return {
                "name": name,
                "percent_off": percent_off,
                "duration_months": duration_months or intervals,
                "amount": _minor_amount(discount.get("amount")),
                "currency": str(discount.get("currency") or "").strip().upper(),
            }
    return None


def _validate_checkout_eligibility(payload: dict[str, Any], *, promo_echo: Any = None) -> dict[str, Any]:
    """Require authoritative Stripe evidence before requesting a BLIK code.

    ``promo_echo`` is the ``promo_campaign`` object the creation response
    returned; it is only used to explain a non-zero amount, so a run with a
    correct 0 PLN total is unaffected.
    """
    methods = _payment_method_types(payload)
    if "blik" not in methods:
        raise BlikEligibilityError(
            "Stripe Checkout does not offer BLIK; payment_method_types=" + json.dumps(methods),
            "blik_method_unavailable",
            payment_method_types=methods,
        )

    total = payload.get("total_summary")
    if not isinstance(total, dict):
        raise BlikEligibilityError(
            "Stripe Checkout did not return total_summary; amount cannot be verified",
            "blik_amount_unknown",
            payment_method_types=methods,
        )

    currency = str(payload.get("currency") or total.get("currency") or "").strip().upper()
    if currency != "PLN":
        raise BlikEligibilityError(
            "BLIK Checkout currency must be PLN; observed " + (currency or "unknown"),
            "blik_currency_mismatch",
            currency=currency or None,
            payment_method_types=methods,
        )

    due = _minor_amount(total.get("due"))
    if due is None:
        raise BlikEligibilityError(
            "Stripe Checkout did not return a verifiable total_summary.due amount",
            "blik_amount_unknown",
            currency=currency,
            payment_method_types=methods,
        )
    if due != 0:
        # Say whether the checkout session itself carried the promo, so a
        # dropped discount is distinguishable from an account that simply is
        # not eligible for the free trial.
        suffix = ""
        if promo_echo is not None:
            suffix = "; checkout promo_campaign=" + json.dumps(
                promo_echo, ensure_ascii=False, separators=(",", ":"),
            )
        raise BlikEligibilityError(
            f"BLIK Checkout is not free; total_summary.due={due} {currency}" + suffix,
            "blik_not_zero_due",
            amount=due,
            currency=currency,
            payment_method_types=methods,
        )

    subtotal = _minor_amount(total.get("subtotal"))
    discount_total = _minor_amount(total.get("total_discount_amount_aggregate"))
    trial = _trial_discount(total)
    if trial is None:
        raise BlikEligibilityError(
            "Account is not eligible for the ChatGPT Plus 1 Month Free Trial",
            "blik_promo_ineligible",
            amount=due,
            currency=currency,
            payment_method_types=methods,
            promo_applied=False,
            promo_eligible=False,
        )

    trial_amount = trial.get("amount")
    discount_covers_subtotal = (
        subtotal is not None
        and subtotal > 0
        and discount_total is not None
        and discount_total >= subtotal
        and trial_amount is not None
        and trial_amount >= subtotal
    )
    trial_currency = str(trial.get("currency") or currency).upper()
    if not discount_covers_subtotal or trial_currency != "PLN":
        raise BlikEligibilityError(
            "1 Month Free Trial was present but was not fully applied to the PLN Checkout",
            "blik_promo_not_applied",
            amount=due,
            currency=currency,
            payment_method_types=methods,
            promo_applied=False,
            promo_eligible=True,
        )

    return {
        "amount": due,
        "currency": currency,
        "payment_method_types": methods,
        "promo_applied": True,
        "promo_eligible": True,
        "promo_name": trial["name"],
        "promo_percent_off": trial["percent_off"],
        "promo_duration_months": trial["duration_months"],
        "subtotal": subtotal,
        "discount_amount": discount_total,
    }


def _initialize_stripe_checkout(session: Any, checkout: dict[str, Any], fp: dict[str, Any], timeout: float) -> None:
    """Initialize Custom Checkout and select its public Stripe-hosted page."""
    cs = str(checkout.get("id") or "")
    pk = str(checkout.get("publishable_key") or "")
    if not pk.startswith("pk_"):
        raise RuntimeError("Checkout response did not include a Stripe publishable key")
    stripe_js_id = str(uuid.uuid4())
    body = {
        "browser_locale": "pl-PL",
        "browser_timezone": "Europe/Warsaw",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "pl",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": pk,
        "_stripe_version": _STRIPE_VERSION,
    }
    emit("stripe_init", "run", "初始化 Stripe BLIK Payment Element")
    response = session.post(
        _STRIPE_PAGE_URL.format(checkout_session_id=cs) + "/init",
        data=body,
        headers=_stripe_headers(cs, fp),
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError("Stripe init rejected HTTP " + str(response.status_code) + ": " + _response_detail(response))
    payload = response.json() or {}
    eligibility = _validate_checkout_eligibility(payload, promo_echo=checkout.get("promo_echo"))
    hosted_url = str(payload.get("stripe_hosted_url") or "").strip()
    if not hosted_url.startswith(("https://checkout.stripe.com/", "https://pay.openai.com/")):
        raise RuntimeError("Stripe init did not return a hosted Checkout URL")
    checkout["browser_url"] = hosted_url
    checkout["stripe_js_id"] = stripe_js_id
    checkout["stripe_poll_params"] = dict(body)
    checkout.update(eligibility)
    emit(
        "stripe_init",
        "ok",
        f"BLIK/PLN 已校验：应付={eligibility['amount']}，1 个月 100% 试用优惠已应用，准备等待动态码",
    )


def _visible_locator(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for scope in [page, *_iter_frames(page)]:
        for selector in selectors:
            try:
                locator = scope.locator(selector).first
                if locator.count() and locator.is_visible() and locator.is_enabled():
                    return locator
            except Exception:
                pass
    return None


def _wait_locator(page: Any, selectors: tuple[str, ...], timeout_seconds: float) -> Any | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        found = _visible_locator(page, selectors)
        if found is not None:
            return found
        page.wait_for_timeout(350)
    return None


def _is_editable_blik_input(locator: Any) -> bool:
    try:
        metadata = locator.evaluate(
            """element => ({
                tag: element.tagName.toLowerCase(),
                type: String(element.type || '').toLowerCase(),
                readOnly: Boolean(element.readOnly),
                disabled: Boolean(element.disabled)
            })"""
        )
    except Exception:
        return False
    return bool(
        isinstance(metadata, dict)
        and metadata.get("tag") == "input"
        and metadata.get("type") in {"", "text", "tel", "number"}
        and metadata.get("readOnly") is not True
        and metadata.get("disabled") is not True
    )


def _visible_blik_code_input(page: Any) -> Any | None:
    for scope in [page, *_iter_frames(page)]:
        for selector in _BLIK_CODE_SELECTORS:
            try:
                matches = scope.locator(selector)
                for index in range(matches.count()):
                    locator = matches.nth(index)
                    if locator.is_visible() and locator.is_enabled() and _is_editable_blik_input(locator):
                        return locator
            except Exception:
                pass
    return None


def _wait_blik_code_input(page: Any, timeout_seconds: float) -> Any | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        found = _visible_blik_code_input(page)
        if found is not None:
            return found
        page.wait_for_timeout(350)
    return None


def _click_blik_submit(page: Any, code_input: Any) -> str:
    candidates: list[tuple[str, Any]] = []
    scopes = [page, *_iter_frames(page)]
    for scope in scopes:
        try:
            candidates.append(("hosted-payment-submit-button", scope.locator(_SUBMIT_SELECTORS[0])))
        except Exception:
            pass
    try:
        form = code_input.locator("xpath=ancestor::form[1]")
        if form.count():
            candidates.extend([
                ("blik-form button[type=submit]", form.locator('button[type="submit"]')),
                ("blik-form input[type=submit]", form.locator('input[type="submit"]')),
            ])
    except Exception:
        pass
    for scope in scopes:
        for selector in _SUBMIT_SELECTORS[1:]:
            try:
                candidates.append((selector, scope.locator(selector)))
            except Exception:
                pass
    for label, matches in candidates:
        try:
            for index in range(matches.count()):
                locator = matches.nth(index)
                if not locator.is_visible() or not locator.is_enabled():
                    continue
                locator.click(timeout=5000)
                page.wait_for_timeout(700)
                return label
        except Exception:
            continue
    return ""


def _blik_response_state(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, str] = {}
    approval = str(payload.get("result") or "").strip().lower()
    if approval:
        result["approval"] = approval

    submission = payload.get("submission_attempt")
    if isinstance(submission, dict):
        submission_state = str(submission.get("state") or "").strip().lower()
        if submission_state:
            result["submission"] = submission_state
        submission_error = submission.get("error")
        if isinstance(submission_error, dict):
            error_code = str(submission_error.get("code") or submission_error.get("decline_code") or "").strip()
            if error_code:
                result["error"] = error_code
            decline_code = str(submission_error.get("decline_code") or "").strip()
            if decline_code:
                result["decline_code"] = decline_code
            message = str(submission_error.get("message") or "").strip()
            if message:
                result["error_message"] = message

    setup = payload.get("setup_intent") if isinstance(payload.get("setup_intent"), dict) else payload
    status = str(setup.get("status") or payload.get("payment_object_status") or "").strip().lower()
    if status:
        result["status"] = status
    error = setup.get("last_setup_error") if isinstance(setup, dict) else None
    if isinstance(error, dict):
        error_code = str(error.get("code") or error.get("decline_code") or "").strip()
        if error_code:
            result["error"] = error_code
        decline_code = str(error.get("decline_code") or "").strip()
        if decline_code:
            result["decline_code"] = decline_code
        message = str(error.get("message") or "").strip()
        if message:
            result["error_message"] = message
    return result


def _blik_state_detail(state: dict[str, str]) -> str:
    """Build a short diagnostic from Stripe's payment/setup error fields."""
    if not isinstance(state, dict):
        return ""
    parts: list[str] = []
    for key in ("error", "decline_code", "error_message"):
        value = str(state.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return ": ".join(parts)[:240]


def _valid_client_observation(value: Any) -> bool:
    return bool(re.fullmatch(r"v1\.r\.p\.[A-Za-z0-9._~-]+", str(value or "").strip()))


def _browser_cookie_header(context: Any, fallback: str) -> str:
    try:
        parts = []
        for item in context.cookies("https://chatgpt.com/"):
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if name and value and "\r" not in value and "\n" not in value:
                parts.append(name + "=" + value)
        if parts:
            return "; ".join(parts)
    except Exception:
        pass
    return fallback


def _browser_fetch_json(page: Any, url: str, target_path: str, target_route: str = "") -> tuple[int, dict[str, Any]]:
    if page is None:
        return 0, {}
    try:
        result = page.evaluate(
            """async ({url, path, route}) => {
              try {
                const response = await fetch(url, {
                  credentials: 'include',
                  headers: {
                    'accept': '*/*',
                    'x-openai-target-path': path,
                    'x-openai-target-route': route || path
                  }
                });
                return {status: response.status, text: await response.text()};
              } catch (error) {
                return {status: 0, text: '', error: String(error)};
              }
            }""",
            {"url": url, "path": target_path, "route": target_route},
        ) or {}
        status = int(result.get("status") or 0)
        payload = json.loads(str(result.get("text") or "{}"))
        return status, payload if isinstance(payload, dict) else {}
    except Exception:
        return 0, {}


def _capture_browser_header_context(target: dict[str, str], request: Any) -> None:
    """Capture signed deployment headers without retaining request bodies."""
    try:
        raw = request.all_headers()
    except Exception:
        raw = getattr(request, "headers", {}) or {}
    headers = {str(key).lower(): str(value).strip() for key, value in dict(raw).items()}
    attestation = headers.get("oai-web-deployment-attestation", "")
    observation = headers.get("x-oai-is-client-observation", "")
    if attestation:
        target["attestation"] = attestation
    if _valid_client_observation(observation):
        target["observation"] = observation
    for source, destination in (
        ("oai-client-version", "client_version"),
        ("oai-client-build-number", "client_build"),
        ("oai-telemetry", "telemetry"),
    ):
        if headers.get(source):
            target[destination] = headers[source]


def _browser_approval_risk(page: Any, device_id: str) -> dict[str, str]:
    """Mint approval Sentinel/SO inside the live ChatGPT Chromium page."""
    if page is None:
        return {}
    try:
        result = page.evaluate(
            """async ({flow}) => {
              try {
                if (!globalThis.SentinelSDK || typeof globalThis.SentinelSDK.token !== 'function') {
                  const script = document.createElement('script');
                  script.src = '/backend-api/sentinel/sdk.js';
                  document.head.appendChild(script);
                  await new Promise((resolve, reject) => {
                    script.onload = resolve;
                    script.onerror = () => reject(new Error('Sentinel SDK load failed'));
                  });
                }
                for (let i = 0; i < 60; i++) {
                  if (globalThis.SentinelSDK && typeof globalThis.SentinelSDK.token === 'function') break;
                  await new Promise(resolve => setTimeout(resolve, 100));
                }
                const sdk = globalThis.SentinelSDK;
                if (!sdk || typeof sdk.token !== 'function') throw new Error('Sentinel SDK unavailable');
                if (typeof sdk.init === 'function') await sdk.init(flow);
                const token = await Promise.race([
                  sdk.token(flow),
                  new Promise((_, reject) => setTimeout(() => reject(new Error('Sentinel token timeout')), 10000))
                ]);
                await new Promise(resolve => setTimeout(resolve, 650));
                const so = typeof sdk.sessionObserverToken === 'function'
                  ? await Promise.race([
                      sdk.sessionObserverToken(flow),
                      new Promise((_, reject) => setTimeout(() => reject(new Error('SO token timeout')), 5000))
                    ]).catch(() => null)
                  : null;
                try {
                  await fetch('/backend-api/sentinel/ping', {
                    method: 'POST', credentials: 'include', headers: {'accept': '*/*'}
                  });
                } catch (_) {}
                return {
                  token: typeof token === 'string' ? token : JSON.stringify(token || {}),
                  so: typeof so === 'string' ? so : JSON.stringify(so || {})
                };
              } catch (error) {
                return {error: String(error)};
              }
            }""",
            {"flow": "checkout_session_approval"},
        ) or {}
        token_value = str(result.get("token") or "").strip()
        so_value = str(result.get("so") or "").strip()
        token = json.loads(token_value or "{}")
        so = json.loads(so_value or "{}")
        if not isinstance(token, dict) or token.get("id") != device_id:
            return {}
        if token.get("flow") != "checkout_session_approval" or not token.get("p") or not token.get("c"):
            return {}
        out = {"openai-sentinel-token": token_value}
        if isinstance(so, dict) and so.get("so"):
            out["OpenAI-Sentinel-SO-Token"] = so_value
        return out
    except Exception:
        return {}


def _normalize_sentinel_headers(risk: dict[str, str]) -> dict[str, str]:
    """Keep main Sentinel and session-observer proofs in separate HAR headers."""
    out = dict(risk or {})
    value = str(out.get("openai-sentinel-token") or "").strip()
    try:
        token = json.loads(value)
    except Exception:
        return out
    if not isinstance(token, dict):
        return out
    embedded_so = token.pop("_so", None)
    if embedded_so and not out.get("OpenAI-Sentinel-SO-Token"):
        try:
            out["OpenAI-Sentinel-SO-Token"] = json.dumps(
                embedded_so, ensure_ascii=False, separators=(",", ":"),
            )
        except Exception:
            pass
    out["openai-sentinel-token"] = json.dumps(token, ensure_ascii=False, separators=(",", ":"))
    return out


def _approve_checkout(session: Any, checkout: dict[str, Any], access_token: str, proxy: str,
                      fp: dict[str, Any], cookies: str, timeout: float, *,
                      browser_page: Any = None, browser_risk: dict[str, str] | None = None) -> tuple[str, str]:
    device_id = str(fp.get("device_id") or "")
    session_id = str(fp.get("oai_session_id") or fp.get("session_seed") or device_id)
    browser_risk = browser_risk if isinstance(browser_risk, dict) else {}
    initial_risk = checkout.get("_checkout_risk") if isinstance(checkout.get("_checkout_risk"), dict) else {}
    attestation = str(browser_risk.get("attestation") or initial_risk.get("oai-web-deployment-attestation") or "").strip()
    last_error = ""

    for attempt in range(2):
        emit("sentinel", "run", "准备生成审批阶段 Sentinel/SO（第 " + str(attempt + 1) + "/2 次）")
        risk = {}
        if browser_page is not None:
            risk = _browser_approval_risk(browser_page, device_id)
            if risk:
                emit(
                    "sentinel", "ok",
                    "已由同会话无头浏览器生成审批 Sentinel/SO（SO="
                    + ("有" if risk.get("OpenAI-Sentinel-SO-Token") else "无") + "）",
                )
        if not risk:
            emit("sentinel", "run", "跳过审批风控浏览器采集，使用 Sentinel SDK bridge 生成令牌")
            try:
                risk = _approval_risk_headers(
                    proxy, device_id, session_id, fp, cookies,
                    access_token=access_token,
                    entity=str(checkout.get("entity") or "openai_ie"),
                    checkout_session_id=str(checkout.get("id") or ""),
                    timeout=timeout,
                    emit=emit,
                    sentinel_cache={"sentinel": "", "so": "", "attestation": attestation},
                )
            except Exception as exc:
                last_error = type(exc).__name__ + ": " + str(exc)[:180]
                continue
        if attestation:
            risk["oai-web-deployment-attestation"] = attestation
        risk = _normalize_sentinel_headers(risk)

        presence = {
            "Sentinel": bool(risk.get("openai-sentinel-token")),
            "SO": bool(risk.get("OpenAI-Sentinel-SO-Token")),
            "部署证明": bool(risk.get("oai-web-deployment-attestation")),
        }
        emit(
            "approve_risk",
            "ok" if all(presence.values()) else "warn",
            "审批风控材料：" + " ".join(name + "=" + ("有" if present else "无") for name, present in presence.items()),
        )
        missing = [name for name, present in presence.items() if not present]
        if missing:
            last_error = "审批风控材料缺失：" + "、".join(missing)
            # Keep the HAR-compatible request path even when an optional
            # browser proof could not be captured.  The upstream response is
            # authoritative; do not turn a recoverable browser-capture miss
            # into a local hard stop.
            emit("approve", "warn", last_error + "；跳过材料补采，直接提交审批请求")

        # HAR 2026-09-09 (approve): the approval request carries the Sentinel
        # token, the session-observer token, the deployment attestation, the
        # deployment client identity, oai-telemetry and - when the session
        # observer produced one - the signed client-observation proof.  Only a
        # genuine "v1.r.p." value captured from the live session is forwarded;
        # the header is never fabricated with a placeholder such as "true".
        extra = {
            **_client_identity(),
            **risk,
        }
        observation = str(browser_risk.get("observation") or "").strip()
        if _valid_client_observation(observation):
            extra["x-oai-is-client-observation"] = observation
        telemetry = str(browser_risk.get("telemetry") or "").strip()
        if telemetry:
            extra["oai-telemetry"] = telemetry
        body = {"checkout_session_id": checkout["id"], "processor_entity": checkout["entity"]}
        try:
            if browser_page is not None:
                # Run from the real ChatGPT origin so TLS, cookies and Fetch
                # metadata remain the same as the page that issued the signed
                # deployment/observation proofs.
                browser_headers = {
                    "accept": "*/*",
                    "content-type": "application/json",
                    "authorization": "Bearer " + access_token,
                    "oai-device-id": device_id,
                    "oai-session-id": session_id,
                    "oai-language": "pl-PL",
                    "x-openai-target-path": "/backend-api/payments/checkout/approve",
                    "x-openai-target-route": "/backend-api/payments/checkout/approve",
                    **extra,
                }
                account_id = _jwt_account_id(access_token)
                if account_id:
                    browser_headers["chatgpt-account-id"] = account_id
                result = browser_page.evaluate(
                    """async ({url, body, headers}) => {
                      try {
                        const response = await fetch(url, {
                          method: 'POST', credentials: 'include', headers,
                          body: JSON.stringify(body)
                        });
                        return {status: response.status, text: await response.text()};
                      } catch (error) {
                        return {status: 0, error: String(error)};
                      }
                    }""",
                    {"url": _APPROVE_URL, "body": body, "headers": browser_headers},
                ) or {}
                status = int(result.get("status") or 0)
                text = str(result.get("text") or "")
                if not status:
                    raise RuntimeError(str(result.get("error") or "browser fetch failed"))
                if status >= 400:
                    last_error = "HTTP " + str(status) + ": " + text[:180]
                    if attempt == 0 and status in {403, 429}:
                        continue
                    return "", last_error
                payload = json.loads(text or "{}")
            else:
                headers = _chatgpt_headers(
                    access_token,
                    "/backend-api/payments/checkout/approve",
                    referer=str(checkout.get("url") or "https://chatgpt.com/"),
                    device_id=device_id,
                    session_id=session_id,
                    fingerprint=fp,
                    cookie_header=cookies,
                    extra=extra,
                )
                response = session.post(_APPROVE_URL, json=body, headers=headers, timeout=timeout)
                if response.status_code >= 400:
                    last_error = "HTTP " + str(response.status_code) + ": " + _response_detail(response)[:180]
                    if attempt == 0 and response.status_code in {403, 429}:
                        continue
                    return "", last_error
                payload = response.json() or {}
        except Exception as exc:
            last_error = type(exc).__name__ + ": " + str(exc)[:180]
            if attempt == 0:
                continue
            return "", last_error
        approval = str(payload.get("result") or payload.get("status") or "").strip().lower()
        # HAR/文档明确约定：blocked 是业务/风控终态，HTTP 200 不能误判为成功，
        # 也不能用换 Sentinel 令牌的方式无脑重放。直接返回，由上层标记为不可重试。
        if approval in {"blocked", "risk_blocked"}:
            detail = str(
                payload.get("reason") or payload.get("message") or payload.get("error") or ""
            ).strip()
            return approval, detail[:240]
        if approval == "expired" and attempt == 0:
            emit("approve", "run", "审批令牌已过期，刷新审批令牌后重试一次")
            continue
        return approval, ""
    return "", last_error or "approval failed"


def _page_diagnostics(page: Any) -> str:
    """Return a short, non-sensitive snapshot when Checkout controls are absent."""
    try:
        title = str(page.title() or "")[:120]
    except Exception:
        title = ""
    try:
        url = str(page.url or "")[:180]
    except Exception:
        url = ""
    try:
        counts = page.locator("iframe").count(), page.locator("input").count(), page.locator("button").count()
    except Exception:
        counts = (0, 0, 0)
    try:
        text = re.sub(r"\s+", " ", str(page.locator("body").inner_text(timeout=1500) or "")).strip()[:260]
    except Exception:
        text = ""
    return f"url={url} title={title!r} iframes={counts[0]} inputs={counts[1]} buttons={counts[2]} text={text!r}"


def _has_active_plus(value: Any) -> bool:
    if isinstance(value, dict):
        if str(value.get("plan_type") or "").lower() == "plus" and value.get("is_delinquent") is not True:
            return True
        return any(_has_active_plus(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_active_plus(item) for item in value)
    return False


def _checkout_is_paid(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and str(value.get("payment_status") or "").strip().lower() == "paid"
        and str(value.get("status") or "").strip().lower() in {"complete", "completed"}
    )


def _wait_plus(session: Any, auth: "_ChatGptSession", fp: dict[str, Any], timeout: float, paid_timeout: float,
               *, browser_page: Any = None, browser_risk: dict[str, str] | None = None,
               cookies: str = "", refresh_token: Any = None,
               referer: str = "https://chatgpt.com/payments/success") -> bool:
    browser_risk = browser_risk if isinstance(browser_risk, dict) else {}
    # HAR entry 248: the checkout page refreshes its cookie session with
    # workspace_update=true&reason=checkout_success before the app reads the new
    # subscription, so adopt the refreshed token before polling for Plus.
    fresh = _refresh_workspace_session(session, fp, cookies, timeout, referer=referer)
    if fresh and fresh != auth.access_token:
        auth.access_token = fresh
        emit("session_refresh", "ok", "结算成功后已刷新会话（HAR: reason=checkout_success）")
    account_id = _jwt_account_id(auth.access_token)
    if not account_id:
        return False
    deadline = time.monotonic() + max(30.0, paid_timeout)
    while time.monotonic() < deadline:
        status, payload = _browser_fetch_json(
            browser_page,
            _SUBSCRIPTIONS_URL + "?account_id=" + account_id,
            "/backend-api/subscriptions",
        )
        if status < 400 and _has_active_plus(payload):
            emit("subscriptions", "ok", "Plus 已到账（浏览器校验 plan_type=plus）")
            return True
        # HAR subscriptions read: deployment client identity, the signed
        # client-observation header, and account_id in the query string (not in
        # a Chatgpt-Account-Id header). The headers are rebuilt every pass so a
        # mid-run token refresh is picked up at once.
        headers = _chatgpt_headers(
            auth.access_token, "/backend-api/subscriptions", device_id=str(fp["device_id"]),
            session_id=str(fp.get("oai_session_id") or fp["session_seed"]), fingerprint=fp,
            cookie_header=cookies,
            extra={
                **_client_identity(),
                "x-oai-is-client-observation": browser_risk.get("observation", ""),
            },
        )
        headers.pop("Chatgpt-Account-Id", None)
        try:
            response = session.get(_SUBSCRIPTIONS_URL, params={"account_id": account_id}, headers=headers, timeout=timeout)
            if int(getattr(response, "status_code", 0) or 0) == 401 and callable(refresh_token):
                if refresh_token():
                    continue
            if response.status_code < 400 and _has_active_plus(response.json()):
                emit("subscriptions", "ok", "Plus 已到账（plan_type=plus）")
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def _refresh_stripe_payment_page(session: Any, checkout: dict[str, Any], fp: dict[str, Any],
                                 timeout: float) -> bool:
    """Re-read the Stripe payment page after ChatGPT approval.

    The HAR runs confirm -> sentinel/req -> approve -> payment_pages GET, so the
    Elements payload is re-read once the manual approval has been recorded.  It
    reuses the exact elements_session_client parameters captured by
    ``_initialize_stripe_checkout``.
    """
    cs = str(checkout.get("id") or "")
    params = checkout.get("stripe_poll_params")
    if not cs or not isinstance(params, dict) or not params:
        return False
    try:
        response = session.get(
            _STRIPE_PAGE_URL.format(checkout_session_id=cs),
            params=params,
            headers=_stripe_headers(cs, fp),
            timeout=min(timeout, 30),
        )
    except Exception as exc:
        emit("stripe_refresh", "warn", "刷新 Stripe payment page 失败：" + type(exc).__name__)
        return False
    if response.status_code >= 400:
        emit("stripe_refresh", "warn", "刷新 Stripe payment page 被拒 HTTP " + str(response.status_code))
        return False
    emit("stripe_refresh", "ok", "已按 HAR 顺序刷新 Stripe payment page")
    return True


def _open_checkout_verify_page(page: Any, checkout: dict[str, Any]) -> bool:
    """Load the ChatGPT verify page Stripe redirects to once the bank approves.

    Best effort and never fatal: the console still verifies the paid state and
    the Plus plan through the backend APIs, so a refused navigation only costs
    the extra browser-side confirmation the HAR shows.
    """
    if page is None:
        return False
    try:
        page.goto(_checkout_verify_url(checkout), wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        emit("checkout_verify", "warn", "打开 /checkout/verify 失败：" + type(exc).__name__)
        return False
    if _CHECKOUT_VERIFY_PATH not in str(page.url or ""):
        emit("checkout_verify", "warn", "verify 页被重定向，继续以接口轮询为准")
        return False
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass
    return True


def _configure_playwright_node() -> None:
    """Use a working system Node for Playwright's driver on Windows.

    Some Python Playwright installations bundle a node.exe that cannot resolve
    the protected user-profile path in this deployment. The normal PATH Node
    (used by the console itself) is equivalent for the driver protocol.
    """
    if os.environ.get("PLAYWRIGHT_NODEJS_PATH"):
        return
    node = shutil.which("node") or shutil.which("node.exe")
    if node:
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = node


def _run_browser(session: Any, checkout: dict[str, Any], cookies: str, access_token: str,
                 proxy: str, fp: dict[str, Any], billing: dict[str, Any], timeout: float,
                 paid_timeout: float, headless: bool) -> dict[str, Any]:
    _configure_playwright_node()
    from playwright.sync_api import sync_playwright

    account_id = _jwt_account_id(access_token)
    responses: list[Any] = []
    request_urls: list[str] = []
    request_failures: list[str] = []
    billing_sync = {"taxes": False, "snapshot": False}
    bridge_holder: list[Any] = []
    with sync_playwright() as playwright:
        launch: dict[str, Any] = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
        executable = _chromium_executable()
        if executable:
            _prepare_chromium_runtime(executable)
        browser_proxy = _proxy_for_playwright(proxy, bridge_holder)
        if browser_proxy:
            launch["proxy"] = browser_proxy
        browser = _launch_playwright_browser(playwright, launch)
        try:
            screen = str(fp.get("screen") or "1920x1080").lower().split("x")
            viewport = {"width": int(screen[0]), "height": int(screen[1])} if len(screen) == 2 else {"width": 1365, "height": 900}
            context_options: dict[str, Any] = {
                "locale": "pl-PL", "timezone_id": "Europe/Warsaw", "viewport": viewport,
                "device_scale_factor": float(fp.get("device_scale_factor") or 1),
                "extra_http_headers": {"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"},
            }
            if str(fp.get("ua") or "").strip():
                context_options["user_agent"] = str(fp["ua"])
            context = browser.new_context(**context_options)
            context.add_init_script(_fingerprint_init_script(fp))
            _add_cookie_header(context, cookies)
            browser_risk: dict[str, str] = {}
            initial_risk = checkout.get("_checkout_risk")
            if isinstance(initial_risk, dict):
                browser_risk.update({str(key): str(value) for key, value in initial_risk.items() if value})
            auth = _ChatGptSession(access_token)
            verify_opened = False

            def refresh_session() -> bool:
                referer = (
                    _checkout_verify_url(checkout) if verify_opened
                    else str(checkout.get("url") or "https://chatgpt.com/")
                )
                return auth.refresh(
                    session, fp, _browser_cookie_header(context, cookies), timeout,
                    referer=referer,
                )

            def authorize(route: Any) -> None:
                request = route.request
                try:
                    parts = urlsplit(request.url)
                    if (parts.hostname or "") == "chatgpt.com":
                        headers = dict(request.headers)
                        # /api/auth/session is cookie-authenticated and is exactly
                        # what the real client calls after a token_expired 401;
                        # injecting the stale Bearer there would defeat the
                        # refresh.  Every other ChatGPT call carries it.
                        if parts.path.startswith("/api/auth/session"):
                            headers.pop("authorization", None)
                        else:
                            headers["authorization"] = "Bearer " + auth.access_token
                        headers["oai-device-id"] = str(fp["device_id"])
                        headers["oai-session-id"] = str(fp.get("oai_session_id") or fp["session_seed"])
                        headers["oai-language"] = "pl-PL"
                        if account_id:
                            headers["chatgpt-account-id"] = account_id
                        route.continue_(headers=headers)
                        return
                except Exception:
                    pass
                route.continue_()

            context.route("**/*", authorize)
            watched_markers = (
                "/payment_pages/", "/setup_intents/", "/backend-api/payments/checkout/",
                "/backend-api/subscriptions", "/backend-api/payments/checkout/taxes",
                "/backend-api/payments/checkout/snapshot",
            )

            def watch_response(response: Any) -> None:
                if "/backend-api/payments/checkout/taxes" in response.url:
                    billing_sync["taxes"] = int(getattr(response, "status", 0) or 0) < 400
                elif "/backend-api/payments/checkout/snapshot" in response.url:
                    billing_sync["snapshot"] = int(getattr(response, "status", 0) or 0) < 400
                if any(marker in response.url for marker in watched_markers):
                    responses.append(response)

            def watch_request(request: Any) -> None:
                if any(marker in request.url for marker in watched_markers):
                    request_urls.append(str(request.url))

            def watch_requestfailed(request: Any) -> None:
                if any(marker in request.url for marker in watched_markers):
                    request_failures.append(str(request.failure or "request_failed")[:160])

            # HAR keeps the ChatGPT checkout tab open next to the Stripe
            # element: approve is issued from that tab (its Referer is
            # /checkout/{entity}/{cs}), and the same tab is what attaches the
            # deployment attestation, the client observation proof and
            # oai-telemetry to the request.  Stripe's hosted page below stays
            # the source for hCaptcha/init_checksum.  Both tabs feed the same
            # watch list, so an approve that the ChatGPT page issues on its own
            # is observed and the 8-second manual fallback does not double-send.
            risk_page = None
            try:
                risk_page = context.new_page()
                risk_page.on(
                    "request",
                    lambda request: _capture_browser_header_context(browser_risk, request),
                )
                risk_page.on("response", watch_response)
                risk_page.on("request", watch_request)
                risk_page.on("requestfailed", watch_requestfailed)
                risk_page.goto(str(checkout.get("url") or ""), wait_until="domcontentloaded", timeout=int(timeout * 1000))
                if "/auth/login" in str(risk_page.url or ""):
                    raise RuntimeError("ChatGPT checkout page redirected to login")
            except Exception as exc:
                risk_page = None
                emit("checkout_page", "warn", "ChatGPT 结算页不可用，风控材料改由接口侧生成：" + type(exc).__name__)
            emit(
                "approve_risk",
                "ok" if browser_risk.get("observation") else "warn",
                "浏览器风控材料：" + " ".join(
                    name + "=" + ("有" if browser_risk.get(key) else "无")
                    for name, key in (
                        ("部署证明", "attestation"),
                        ("观测值", "observation"),
                        ("telemetry", "telemetry"),
                        ("客户端身份", "client_version"),
                    )
                ),
            )
            page = context.new_page()
            page.on("request", watch_request)
            page.on("response", watch_response)
            page.on("requestfailed", watch_requestfailed)
            emit("checkout_page", "run", "加载 Stripe Checkout 页面生成 BLIK 支付参数")
            page.goto(checkout.get("browser_url") or checkout["url"], wait_until="domcontentloaded", timeout=int(timeout * 1000))
            if "/auth/login" in str(page.url or ""):
                raise RuntimeError("Checkout 被重定向到登录页：AT 未被浏览器会话接受")
            if not _wait_locator(page, _BLIK_SELECTORS, min(timeout, 90)):
                raise RuntimeError("BLIK payment method was not rendered; " + _page_diagnostics(page))
            if not _click_first_ready(page, _BLIK_SELECTORS):
                raise RuntimeError("BLIK payment method could not be selected")
            address = dict(billing.get("address") or {})
            billing_sync["taxes"] = False
            billing_sync["snapshot"] = False
            _fill_billing_details_if_present(page, {**address, "name": billing.get("name"), "email": billing.get("email")})
            _click_terms_if_present(page)
            sync_deadline = time.monotonic() + min(20.0, max(5.0, timeout))
            while time.monotonic() < sync_deadline and not all(billing_sync.values()):
                page.wait_for_timeout(250)
            if all(billing_sync.values()):
                emit("billing", "ok", "波兰账单已填写，taxes/snapshot 同步完成")
            else:
                missing = ", ".join(key for key, ready in billing_sync.items() if not ready)
                emit("billing", "warn", "账单已填写，但仍未观察到 Stripe " + missing + " 响应；继续等待页面控件")

            code_attempt = 0
            blocked_attempts = 0
            while code_attempt < 3:
                code_input = _wait_blik_code_input(page, 30)
                if code_input is None:
                    raise BlikPageContractError(
                        "BLIK editable code input was not rendered; " + _page_diagnostics(page),
                        "blik_code_input_missing",
                    )
                code_attempt += 1
                event("blik_waiting", attempt=code_attempt, max_attempts=3)
                emit("blik_input", "waiting", f"等待用户输入 6 位 BLIK 动态码（第 {code_attempt}/3 次）")
                line = sys.stdin.readline()
                if not line:
                    raise RuntimeError("BLIK input channel closed")
                try:
                    command = json.loads(line)
                except Exception:
                    command = {}
                code = str(command.get("code") or "").strip()
                if not re.fullmatch(r"\d{6}", code):
                    event("blik_rejected", reason="invalid_format")
                    emit("blik_input", "waiting", "动态码格式错误，请输入 6 位数字")
                    code_attempt -= 1
                    continue
                event("blik_processing", attempt=code_attempt)
                emit("confirm", "run", "由 Stripe.js 提交 BLIK，并刷新被动 hCaptcha 与 init_checksum")
                if not _is_editable_blik_input(code_input):
                    raise BlikPageContractError(
                        "BLIK code field changed before submission",
                        "blik_code_input_invalid",
                    )
                responses.clear()
                request_failures.clear()
                request_urls.clear()
                code_input.fill(code)
                # Stripe's BLIK widget accepts six digits but some deployed
                # versions only enable the submit control after the masked
                # `123 456` value has propagated through input/change events.
                try:
                    code_input.press("Tab")
                except Exception:
                    pass
                submit_control = _click_blik_submit(page, code_input)
                if not submit_control:
                    raise BlikPageContractError(
                        "Checkout submit button was not available",
                        "blik_submit_missing",
                    )
                emit("confirm", "run", "已点击 Stripe 主提交控件：" + submit_control)

                deadline = time.monotonic() + min(180.0, paid_timeout)
                setup_succeeded = False
                checkout_paid = False
                retry_reason = ""
                last_retry_reason = ""
                seen_approval = False
                seen_confirm = False
                seen_bank_authorization = False
                response_error = ""
                approval_required_at = 0.0
                manual_approval_attempted = False
                last_manual_poll = 0.0
                last_checkout_poll = 0.0
                checkout_wait_logged = False
                submit_retry_at = time.monotonic()
                submit_retried = False
                stripe_refreshed = False
                verify_opened = False
                while time.monotonic() < deadline:
                    page.wait_for_timeout(750)
                    if any(f"/payment_pages/{checkout['id']}/confirm" in url for url in request_urls):
                        seen_confirm = True
                    if not seen_confirm and not submit_retried and time.monotonic() - submit_retry_at >= 6:
                        submit_retried = True
                        emit("confirm", "run", "Stripe 未发出确认请求，重新触发 BLIK 输入校验并提交")
                        try:
                            code_input.fill(f"{code[:3]} {code[3:]}")
                            code_input.press("Tab")
                        except Exception:
                            pass
                        _click_blik_submit(page, code_input)
                    pending, responses[:] = responses[:], []
                    for observed in pending:
                        url = observed.url
                        if f"/payment_pages/{checkout['id']}/confirm" in url:
                            seen_confirm = True
                        try:
                            payload = observed.json()
                        except Exception:
                            if int(getattr(observed, "status", 0) or 0) >= 400:
                                response_error = "http_" + str(observed.status)
                            continue
                        state = _blik_response_state(payload)
                        if state.get("submission") == "requires_approval" and not approval_required_at:
                            approval_required_at = time.monotonic()
                            emit("approve", "run", "Stripe confirm 已接受，等待 ChatGPT Checkout 审批")
                        if int(getattr(observed, "status", 0) or 0) >= 400:
                            response_error = state.get("error") or "http_" + str(observed.status)
                        if "/payments/checkout/approve" in url:
                            approval = state.get("approval", "")
                            if approval == "blocked":
                                blocked_attempts += 1
                                detail = _blik_state_detail(state)
                                retry_reason = "approval blocked" + ((": " + detail) if detail else "")
                                last_retry_reason = retry_reason
                            if approval == "approved":
                                seen_approval = True
                                emit("approve", "ok", "ChatGPT 审批通过，等待银行 App 授权")
                        if (
                            f"/backend-api/payments/checkout/{checkout['entity']}/{checkout['id']}" in url
                            and _checkout_is_paid(payload)
                        ):
                            checkout_paid = True
                            emit("checkout_status", "ok", "ChatGPT Checkout 已确认 payment_status=paid")
                        if "/setup_intents/" in url or ("/payment_pages/" in url and isinstance(payload, dict)):
                            status = state.get("status", "")
                            if status == "requires_action":
                                if not seen_bank_authorization:
                                    seen_bank_authorization = True
                                    event("blik_bank_authorization", attempt=code_attempt)
                                    emit("bank_authorization", "waiting", "请在银行 App 中确认 BLIK 支付")
                            elif status == "succeeded":
                                setup_succeeded = True
                            elif status == "canceled":
                                return {
                                    "ok": False,
                                    "error": "BLIK SetupIntent was canceled; a new Checkout is required",
                                    "reason_code": "blik_setup_canceled",
                                    "retryable": True,
                                }
                            elif status == "requires_payment_method":
                                retry_reason = _blik_state_detail(state) or status
                                last_retry_reason = retry_reason
                        if "/backend-api/subscriptions" in url and _has_active_plus(payload):
                            return {"ok": True, "paid": True, "payment_status": "plus_active"}
                    if retry_reason:
                        break
                    current = time.monotonic()
                    if seen_approval and not stripe_refreshed:
                        stripe_refreshed = True
                        _refresh_stripe_payment_page(session, checkout, fp, timeout)
                    if (setup_succeeded or seen_bank_authorization) and not verify_opened:
                        verify_opened = True
                        if _open_checkout_verify_page(page, checkout):
                            emit("checkout_verify", "ok", "已打开 /checkout/verify，等待 ChatGPT 结算状态")
                    if approval_required_at and not seen_approval and not manual_approval_attempted and current - approval_required_at >= 8:
                        manual_approval_attempted = True
                        emit("approve", "run", "页面未自动发起审批，按 HAR 链路提交 ChatGPT approve")
                        approval, approval_error = _approve_checkout(
                            session, checkout, auth.access_token, proxy, fp,
                            _browser_cookie_header(context, cookies), min(timeout, 90),
                            # Approve from the ChatGPT tab so the request is a
                            # same-origin browser fetch, exactly as in the HAR.
                            browser_page=risk_page,
                            browser_risk=browser_risk,
                        )
                        if approval == "approved":
                            seen_approval = True
                            emit("approve", "ok", "ChatGPT 审批通过，等待银行 App 授权")
                        elif approval == "blocked":
                            blocked_attempts += 1
                            detail = approval_error.strip()
                            retry_reason = "approval blocked" + ((": " + detail) if detail else "")
                            last_retry_reason = retry_reason
                        else:
                            return {
                                "ok": False,
                                "error": "BLIK approval failed: " + (approval_error or approval or "unknown result"),
                                "reason_code": "blik_approve_failed",
                                "retryable": True,
                            }
                    if seen_approval and current - last_manual_poll >= 2:
                        last_manual_poll = current
                        try:
                            poll_response = session.get(
                                f"https://api.stripe.com/v1/payment_pages/{checkout['id']}/poll",
                                params={
                                    "key": checkout.get("publishable_key") or "",
                                    "_stripe_version": _STRIPE_VERSION,
                                },
                                headers=_stripe_headers(checkout["id"], fp),
                                timeout=min(timeout, 30),
                            )
                            if poll_response.status_code >= 400:
                                response_error = "poll_http_" + str(poll_response.status_code)
                            else:
                                request_failures.clear()
                                poll_state = _blik_response_state(poll_response.json() or {})
                                poll_status = poll_state.get("status", "")
                                if poll_status == "requires_action" and not seen_bank_authorization:
                                    seen_bank_authorization = True
                                    event("blik_bank_authorization", attempt=code_attempt)
                                    emit("bank_authorization", "waiting", "请在银行 App 中确认 BLIK 支付")
                                elif poll_status == "succeeded":
                                    setup_succeeded = True
                                elif poll_status in {"requires_payment_method", "canceled"}:
                                    retry_reason = poll_state.get("error") or poll_status
                        except Exception as exc:
                            request_failures.append(type(exc).__name__ + ": " + str(exc)[:140])
                    if seen_approval and current - last_checkout_poll >= 3:
                        last_checkout_poll = current
                        checkout_path = f"/backend-api/payments/checkout/{checkout['entity']}/{checkout['id']}"
                        status, payload = _browser_fetch_json(
                            # Read from the ChatGPT tab first: HAR entries
                            # 145/161/226 are same-origin browser fetches from
                            # that page, so they carry its cookies and fetch
                            # metadata.  The requests-based read below stays as
                            # the fallback when the page cannot serve the call.
                            risk_page,
                            "https://chatgpt.com" + checkout_path,
                            checkout_path,
                            "/backend-api/payments/checkout/{processor_entity}/{checkout_session_id}",
                        )
                        if not status or status >= 400:
                            if status == 401:
                                refresh_session()
                            try:
                                checkout_headers = _chatgpt_headers(
                                    auth.access_token, checkout_path,
                                    # HAR entries 145/161/226: once the browser
                                    # sits on /checkout/verify, that URL - not the
                                    # checkout page - is the referer of the
                                    # follow-up checkout reads.
                                    referer=(
                                        _checkout_verify_url(checkout) if verify_opened
                                        else str(checkout.get("url") or "https://chatgpt.com/")
                                    ),
                                    device_id=str(fp["device_id"]),
                                    session_id=str(fp.get("oai_session_id") or fp["session_seed"]),
                                    fingerprint=fp,
                                    cookie_header=_browser_cookie_header(context, cookies),
                                    extra={
                                        **_client_identity(),
                                        "oai-web-deployment-attestation": browser_risk.get("attestation", ""),
                                        "x-oai-is-client-observation": browser_risk.get("observation", ""),
                                        "x-openai-target-route": "/backend-api/payments/checkout/{processor_entity}/{checkout_session_id}",
                                    },
                                )
                                checkout_response = session.get(
                                    "https://chatgpt.com" + checkout_path,
                                    headers=checkout_headers,
                                    timeout=min(timeout, 30),
                                )
                                status = int(checkout_response.status_code)
                                payload = checkout_response.json() if status < 400 else {}
                            except Exception:
                                pass
                        if status < 400 and _checkout_is_paid(payload):
                            checkout_paid = True
                            emit("checkout_status", "ok", "ChatGPT Checkout 已确认 payment_status=paid")
                    if checkout_paid:
                        emit("subscriptions", "run", "订单已支付，查询 Plus 是否到账")
                        if _wait_plus(
                            session, auth, fp, timeout, paid_timeout,
                            browser_page=None,
                            browser_risk=browser_risk,
                            cookies=_browser_cookie_header(context, cookies),
                            refresh_token=refresh_session,
                        ):
                            return {"ok": True, "paid": True, "payment_status": "plus_active"}
                        return {"ok": False, "error": "BLIK succeeded but Plus was not observed before timeout", "reason_code": "blik_plus_timeout", "retryable": False}
                    if setup_succeeded and not checkout_wait_logged:
                        checkout_wait_logged = True
                        emit("checkout_status", "run", "Stripe 已完成，等待 ChatGPT Checkout 确认 paid")
                if retry_reason:
                    event("blik_retry", reason=retry_reason, next_attempt=code_attempt + 1)
                    emit("confirm", "fail", "BLIK 码未获银行接受，请获取新的动态码后重试：" + retry_reason)
                    continue
                if seen_bank_authorization:
                    return {
                        "ok": False,
                        "error": "BLIK bank authorization result timed out",
                        "reason_code": "blik_bank_confirmation_timeout",
                        "retryable": False,
                    }
                if request_failures:
                    return {
                        "ok": False,
                        "error": "BLIK confirm network request failed: " + request_failures[-1],
                        "reason_code": "blik_confirm_network_error",
                        "retryable": True,
                    }
                if response_error:
                    return {
                        "ok": False,
                        "error": "BLIK confirm returned an error: " + response_error,
                        "reason_code": "blik_confirm_http_error",
                        "retryable": False,
                    }
                if not seen_confirm:
                    return {
                        "ok": False,
                        "error": "Stripe Checkout did not send the BLIK confirm request; " + _page_diagnostics(page),
                        "reason_code": "blik_confirm_not_sent",
                        "retryable": False,
                    }
                if approval_required_at and not seen_approval:
                    return {
                        "ok": False,
                        "error": "BLIK ChatGPT approval result timed out",
                        "reason_code": "blik_approval_timeout",
                        "retryable": True,
                    }
                return {
                    "ok": False,
                    "error": "BLIK setup state did not advance after approval",
                    "reason_code": "blik_setup_timeout",
                    "retryable": True,
                }
            detail = (": " + last_retry_reason) if last_retry_reason else ""
            return {
                "ok": False,
                "error": "BLIK code attempts exhausted" + detail,
                "reason_code": "blik_approve_blocked" if blocked_attempts >= 3 else "blik_code_exhausted",
                "retryable": False,
            }
        finally:
            browser.close()
            for bridge in bridge_holder:
                try:
                    bridge.close()
                except Exception:
                    pass


def run(args: argparse.Namespace) -> dict[str, Any]:
    proxy = normalize_proxy_url(args.proxy)
    if not proxy:
        return {"ok": False, "error": "BLIK requires a proxy", "reason_code": "blik_proxy_required", "retryable": False}
    fp = _polish_fingerprint(args.fingerprint, args.email)
    billing = generate_pl_billing(args.email, seed=str(fp.get("device_id") or args.email))
    try:
        emit("config", "run", "BLIK 强制使用 PL/PLN、pl-PL、Europe/Warsaw；代理=" + redact_proxy_url(proxy))
        _verify_polish_exit(proxy)
        session, checkout, cookies = _create_checkout(args.at, proxy, fp, args.promo_campaign_id, args.timeout)
        _initialize_stripe_checkout(session, checkout, fp, args.timeout)
        result = _run_browser(session, checkout, cookies, args.at, proxy, fp, billing,
                              args.timeout, args.paid_timeout, args.headless)
        result.update({
            "checkout_url": checkout["url"],
            "checkout_session_id": checkout["id"],
            "processor_entity": checkout["entity"],
            "billing_country": "PL",
            "currency": checkout["currency"],
            "amount": checkout["amount"],
            "payment_method_types": checkout["payment_method_types"],
            "promo_applied": checkout["promo_applied"],
            "promo_eligible": checkout["promo_eligible"],
            "promo_name": checkout["promo_name"],
            "promo_percent_off": checkout["promo_percent_off"],
            "promo_duration_months": checkout["promo_duration_months"],
            "subtotal": checkout["subtotal"],
            "discount_amount": checkout["discount_amount"],
        })
        if result.get("ok") and result.get("paid"):
            result["link"] = checkout["url"]
        return result
    except BlikEligibilityError as exc:
        return {
            "ok": False,
            "error": str(exc)[:500],
            "reason_code": exc.reason_code,
            "retryable": exc.retryable,
            **exc.details,
        }
    except BlikPageContractError as exc:
        return {
            "ok": False,
            "error": str(exc)[:500],
            "reason_code": exc.reason_code,
            "retryable": exc.retryable,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "reason_code": "blik_flow_failed", "retryable": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--at", required=True)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--email", default="")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--promo-campaign-id", default="plus-1-month-free")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--paid-timeout", type=float, default=300.0)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
