#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OA/CS 链路提链模块 (纯 AT 协议, OpenAI Custom Checkout -> PayPal BA 链)。

已验证 (2026-08-15):
  OA 链路: OA账号(oaics_) + US/DE订单 + 同国出口(GB/JP/TH/DE) 全流程
           -> 0元促销 + paypal方法 + paypal.com/agreements/approve BA 链
  CS 链路: 接口预留 (generate_opll_paypal_global_cslive_confirmation_token_branch 对齐)

用法:
  from sms_tool.oa_link import extract_payment_link
  result = extract_payment_link(access_token, proxy='gate:port:user:pass',
                                billing_country='DE', method='oa')
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover
    curl_requests = None

import requests  # noqa: E402

STRIPE_VERSION = '2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1'
RUNTIME_VERSION = '6f8494a281'
DEFAULT_PK = "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
BA_RE = re.compile(r"https://(?:www\.)?paypal\.com/agreements/approve\?[^\s<>]+", re.I)
PM_RE = re.compile(r"https://(?:www\.)?pm-redirects\.stripe\.com/authorize/[^\s<>]+", re.I)
PAYPAL_PAGE_RE = re.compile(r'''https://(?:www\.)?paypal\.com/[^\s"'<>)]+''', re.I)
CURRENCY = {"US": "USD", "GB": "GBP", "DE": "EUR", "JP": "JPY", "TH": "THB", "VN": "VND", "BR": "BRL"}
BILLING = {
    "US": ("James Smith", "350 5th Ave", "New York", "NY", "10001"),
    "GB": ("James Smith", "10 Downing Street", "London", "London", "SW1A 2AA"),
    "DE": ("Max Mueller", "Invalidenstrasse 1", "Berlin", "Berlin", "10115"),
    "JP": ("Taro Yamada", "1-1 Chiyoda", "Chiyoda-ku", "Tokyo", "100-0001"),
}
PAYPAL_BAD_CURRENCY = {"JPY", "BRL", "THB", "VND"}


class PaymentLinkError(RuntimeError):
    pass


# ─── 代理工具 ────────────────────────────────────────────────────────────────

def normalize_proxy(value: str, default_scheme: str = "http") -> str:
    """归一化代理 URL: 支持 host:port:user:pass / http:// / socks5://"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"{default_scheme}://{text}"
    scheme = text.split("://", 1)[0].lower()
    prefix, rest = text.split("://", 1)
    authority, sep, suffix = rest.partition("/")
    if "@" not in authority and authority.count(":") >= 3:
        host, port, username, password = authority.split(":", 3)
        if host and port.isdigit() and username and password:
            userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}"
            authority = f"{userinfo}@{host}:{port}"
    try:
        parsed = urlsplit(f"{scheme}://{authority}")
        if "kookeey.info" in (parsed.hostname or "").lower() and parsed.port in (1000, 1086) and scheme in ("http", "https"):
            scheme = "socks5h"
    except Exception:
        pass
    return f"{scheme}://{authority}" + (sep + suffix if sep else "")


def proxy_country_hint(proxy: str) -> str:
    """从代理 URL 提取出口国 (region-XX / country-XX 标记)。"""
    raw = str(proxy or "")
    m = re.search(r"(?i)(?:region|country|cc|geo)[-_=:/]+([a-z]{2})(?=$|[^a-z0-9])", raw)
    if m:
        cc = m.group(1).upper()
        if re.fullmatch(r"[A-Z]{2}", cc):
            return cc
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Z]{2})(?=[-_@:/]|$)", raw):
        cc = m.group(1).upper()
        if re.fullmatch(r"[A-Z]{2}", cc) and cc in CURRENCY:
            return cc
    return ""


def probe_proxy_country(proxy: str, timeout: float = 8.0) -> tuple[str, str]:
    """探测代理出口国, 返回 (country, ip)。失败时回退 URL 标记。"""
    normalized = normalize_proxy(proxy)
    try:
        r = requests.get("http://ip-api.com/json/", proxies={"http": normalized, "https": normalized}, timeout=timeout)
        d = r.json() or {}
        return str(d.get("countryCode") or "").upper(), str(d.get("query") or "")
    except Exception:
        return proxy_country_hint(normalized), ""


# ─── HTTP ────────────────────────────────────────────────────────────────────

def _client(proxy: str):
    if curl_requests is not None:
        return curl_requests.Session(impersonate="firefox147", proxy=normalize_proxy(proxy), verify=True)
    s = requests.Session()
    n = normalize_proxy(proxy)
    s.proxies = {"http": n, "https": n}
    return s


_CHROME_UA_OA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# 会话级统一身份（extract_oa_ba_link 内设置）
_OA_SESSION_CTX: dict = {}

def _checkout_post(session, url: str, json_body: dict, access_token: str, extra_headers: dict | None = None, timeout: float = 30.0):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://chatgpt.com/",
        "User-Agent": _CHROME_UA_OA,
        "OAI-Language": "zh-CN",
    }
    # 统一设备身份（对齐 pay153: OAI-Device-Id 贯穿全流程）
    did = _OA_SESSION_CTX.get("device_id")
    if did:
        headers["OAI-Device-Id"] = did
    if extra_headers:
        headers.update(extra_headers)
    return session.post(url, json=json_body, headers=headers, timeout=timeout)


# ─── 提取工具 ────────────────────────────────────────────────────────────────

def deep_first(value, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for k in keys:
            if k in value:
                return value[k]
        for child in value.values():
            found = deep_first(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = deep_first(child, keys)
            if found is not None:
                return found
    return None


def collect_methods(payload: Any) -> list[str]:
    out: list[str] = []
    if isinstance(payload, dict):
        for key in ("payment_method_types", "ordered_payment_method_types", "custom_payment_methods"):
            v = payload.get(key)
            if isinstance(v, list):
                out.extend(str(x) for x in v)
        for child in payload.values():
            out.extend(collect_methods(child))
    elif isinstance(payload, list):
        for child in payload:
            out.extend(collect_methods(child))
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen


def amount_info(payload: Any) -> tuple[int | None, str]:
    if not isinstance(payload, dict):
        return None, ""
    invoice = payload.get("invoice") if isinstance(payload.get("invoice"), dict) else {}
    ts = payload.get("total_summary") if isinstance(payload.get("total_summary"), dict) else {}
    if isinstance(invoice.get("amount_due"), int):
        return invoice["amount_due"], "invoice.amount_due"
    if isinstance(ts.get("due"), int):
        return ts["due"], "total_summary.due"
    return None, ""


def extract_redirect_url(payload: Any) -> str:
    if isinstance(payload, dict):
        na = payload.get("next_action")
        if isinstance(na, dict) and na.get("type") == "redirect_to_url":
            inner = na.get("redirect_to_url")
            if isinstance(inner, dict) and inner.get("url"):
                return str(inner["url"])
        for key in ("setup_intent", "payment_intent"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                found = extract_redirect_url(nested)
                if found:
                    return found
        text = json.dumps(payload, ensure_ascii=False, default=str)
    else:
        text = str(payload)
    m = PM_RE.search(text) or BA_RE.search(text)
    return m.group(0) if m else ""


def session_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for k in ("checkout_session_id", "session_id", "id"):
            v = payload.get(k)
            if isinstance(v, str) and v.startswith(("cs_", "oaics_")):
                return v
    return ""


# ─── OA 链路 (已验证) ────────────────────────────────────────────────────────

def extract_oa_ba_link(
    access_token: str,
    proxy: str,
    billing_country: str = "US",
    promo_proxy: str = "",
    emit: Callable[[str, str], None] | None = None,
    timeout: float = 30.0,
) -> dict:
    """OA 链路: 建单 -> 促销0元 -> elements init -> token -> confirm -> intent confirm -> BA。"""
    log = emit or (lambda stage, msg: print(f"[{stage}] {msg}"))
    country = str(billing_country or "US").strip().upper()
    currency = CURRENCY.get(country, "USD")
    if currency in PAYPAL_BAD_CURRENCY:
        raise PaymentLinkError(f"货币 {currency} 不支持 paypal (仅 eur/gbp/usd/aud/cad/dkk/nok/pln/sek/chf/sgd/nzd)")
    proxy = normalize_proxy(proxy)
    promo = normalize_proxy(promo_proxy) or proxy
    exit_cc, exit_ip = probe_proxy_country(proxy)
    exit_label = exit_cc or proxy_country_hint(proxy) or "?"
    log("config", f"[出口国={exit_label}] [建单国={country}/{currency}] 出口IP={exit_ip or '?'}")
    session = _client(proxy)
    t0 = time.time()

    # ══ 统一设备身份（对齐 pay153 手动提链，修复 confirm_blocked）══
    # pay153: 同一 http 会话(带 oai-did cookie) + 同一 device_id + csrf 暖身
    # 贯穿 create->update->confirm; 我们之前缺这些头导致 OpenAI confirm 风控 blocked
    _oa_device_id = str(uuid.uuid4())
    _oa_did = str(uuid.uuid4())
    _OA_SESSION_CTX["device_id"] = _oa_device_id
    try:
        session.cookies.set("oai-did", _oa_did, domain="chatgpt.com")
    except Exception:
        pass
    try:
        session.get("https://chatgpt.com/api/auth/csrf",
                    headers={"User-Agent": _CHROME_UA_OA,
                             "Accept": "application/json,text/plain,*/*"},
                    timeout=timeout)
    except Exception:
        pass

    # 1) 建单 (custom + promo)
    name, line1, city, state, postal = BILLING.get(country, BILLING["US"])
    email = f"buyer{uuid.uuid4().hex[:8]}@gmail.com"
    body = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "entry_point": "all_plans_pricing_modal",
        "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
        "checkout_ui_mode": "custom",
    }
    r = _checkout_post(session, "https://chatgpt.com/backend-api/payments/checkout", body, access_token, timeout=timeout)
    if r.status_code >= 400:
        raise PaymentLinkError(f"建单失败 HTTP {r.status_code}: {str(r.text)[:300]}")
    data = r.json() or {}
    cs = session_id(data)
    if not cs:
        raise PaymentLinkError(f"建单响应无 cs_id: {str(data)[:300]}")
    if not cs.startswith("oaics_"):
        raise PaymentLinkError(f"非 OA 会话 ({cs[:6]}...): 该账号在此出口非 OA 资格, 可换出口或账号")
    entity = str(data.get("processor_entity") or ("openai_llc" if country == "US" else "openai_ie"))
    pk = str(data.get("publishable_key") or DEFAULT_PK)
    log("checkout", f"[出口国={exit_label}] [建单国={country}] 建单成功 cs={cs[:20]}... entity={entity} ({time.time() - t0:.1f}s)")

    # 2) 促销 update (同出口; 促销池不同则用促销池)
    update_payload = {
        "checkout_session_id": cs,
        "processor_entity": entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {
            "country": country, "currency": currency,
            "name": name, "email": email,
            "address": {"line1": line1, "city": city, "state": state, "postal_code": postal, "country": country},
        },
        "checkout_ui_mode": "custom",
        "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
    }
    headers = {
        "Referer": f"https://chatgpt.com/checkout/{entity}/{cs}",
        "x-openai-target-path": "/backend-api/payments/checkout/update",
        "x-openai-target-route": "/backend-api/payments/checkout/update",
    }
    r = _checkout_post(session, "https://chatgpt.com/backend-api/payments/checkout/update", update_payload,
                       access_token, extra_headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise PaymentLinkError(f"促销 update 失败 HTTP {r.status_code}: {str(r.text)[:200]}")
    upd = r.json() or {}
    promo_cc = exit_label
    if promo != proxy:
        promo_cc, _ = probe_proxy_country(promo)
        promo_cc = promo_cc or proxy_country_hint(promo) or "?"
    log("promo", f"[促销出口={promo_cc}] 促销 update OK ({time.time() - t0:.1f}s)")

    # 3) Elements init (OAICS 专用, 6 次同步重试)
    customer_secret = deep_first(upd, ("customer_session_client_secret", "customerSessionClientSecret", "client_secret"))
    if not customer_secret:
        customer_secret = deep_first(data, ("customer_session_client_secret", "customerSessionClientSecret", "client_secret"))
    if not customer_secret:
        raise PaymentLinkError("促销/建单响应缺少 customer_session_client_secret")
    stripe = _client(promo)
    init_payload = {}
    last_err = ""
    for attempt in range(6):
        params = {
            "client_betas[0]": "custom_checkout_server_updates_1",
            "client_betas[1]": "custom_checkout_manual_approval_1",
            "client_betas[2]": "disable_deferred_intent_client_validation_beta_1",
            "deferred_intent[mode]": "subscription",
            "deferred_intent[amount]": "0",
            "deferred_intent[currency]": currency.lower(),
            "deferred_intent[setup_future_usage]": "off_session",
            "currency": currency.lower(),
            "customer_session_client_secret": customer_secret,
            "key": pk,
            "_stripe_version": STRIPE_VERSION,
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": str(uuid.uuid4()),
            "locale": "en",
            "type": "deferred_intent",
            "checkout_session_id": cs,
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[payment_method_types][1]": "paypal",
            "session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        }
        try:
            resp = stripe.get("https://api.stripe.com/v1/elements/sessions", params=params, timeout=timeout)
            if resp.status_code >= 400:
                last_err = f"HTTP {resp.status_code} {str(resp.text)[:150]}"
            else:
                init_payload = resp.json() or {}
                methods = collect_methods(init_payload)
                amount, _src = amount_info(init_payload)
                if "paypal" in methods and amount == 0:
                    break
                last_err = f"methods={methods} amount={amount}"
        except Exception as exc:
            last_err = str(exc)[:150]
        time.sleep(1.5 if attempt else 0.8)
    if not init_payload:
        raise PaymentLinkError(f"elements init 失败: {last_err}")
    methods = collect_methods(init_payload)
    if "paypal" not in methods:
        raise PaymentLinkError(f"elements init 无 paypal: {methods}")
    amount, amount_src = amount_info(init_payload)
    if amount not in (None, "") and amount != 0:
        raise PaymentLinkError(f"金额非 0: {amount} ({amount_src})")
    log("init", f"[出口国={exit_label}] Elements init OK paypal {methods} amount={amount} ({time.time() - t0:.1f}s)")

    # 4) tax sync
    tax_body = {
        "checkout_session_id": cs,
        "processor_entity": entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {"country": country, "currency": currency},
        "checkout_ui_mode": "custom",
    }
    try:
        r = _checkout_post(session, "https://chatgpt.com/backend-api/payments/checkout/taxes", tax_body,
                           access_token, extra_headers=headers, timeout=timeout)
        if r.status_code < 400:
            log("tax", "tax sync OK")
    except Exception:
        pass

    # 5) confirmation token (mandate_data 带出口 IP)
    acceptance_ip = exit_ip or "0.0.0.0"
    token_body = {
        "payment_method_data[type]": "paypal",
        "payment_method_data[billing_details][name]": name,
        "payment_method_data[billing_details][email]": email,
        "payment_method_data[billing_details][address][country]": country,
        "payment_method_data[billing_details][address][line1]": line1,
        "payment_method_data[billing_details][address][city]": city,
        "payment_method_data[billing_details][address][postal_code]": postal,
        "payment_method_data[billing_details][address][state]": state,
        "payment_method_data[payment_user_agent]": f"stripe.js/{RUNTIME_VERSION}; stripe-js-v3/{RUNTIME_VERSION}; payment-element; deferred-intent",
        "payment_method_data[guid]": str(uuid.uuid4()),
        "payment_method_data[muid]": str(uuid.uuid4()),
        "payment_method_data[sid]": str(uuid.uuid4()),
        "setup_future_usage": "off_session",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][user_agent]": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "mandate_data[customer_acceptance][online][ip_address]": acceptance_ip,
        "client_context[currency]": currency.lower(),
        "client_context[mode]": "subscription",
        "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
        "client_attribution_metadata[checkout_session_id]": cs,
        "client_attribution_metadata[elements_session_id]": f"elements_session_{uuid.uuid4().hex[:11]}",
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "key": pk,
        "_stripe_version": STRIPE_VERSION,
    }
    resp = stripe.post("https://api.stripe.com/v1/confirmation_tokens", data=token_body, timeout=timeout)
    if resp.status_code >= 400:
        raise PaymentLinkError(f"confirmation token 失败 HTTP {resp.status_code}: {str(resp.text)[:250]}")
    token_payload = resp.json() or {}
    ctoken = deep_first(token_payload, ("id", "confirmation_token"))
    if not ctoken or not str(ctoken).startswith("ctoken_"):
        raise PaymentLinkError(f"confirmation token 响应异常: {str(token_payload)[:300]}")
    log("token", f"confirmation token OK ({time.time() - t0:.1f}s)")

    # 6) OpenAI checkout/confirm -> client_secret
    # ══ 对齐 freepp/link-pp 已验证实现（修复 confirm_blocked）══
    # 关键差异: 用 oai-device-id(小写)+oai-session-id+oai-client-version+sec-ch-ua 全套头,
    # 只传 confirm_token(不多传 confirmation_token 字段), 同一 device_id 贯穿。
    try:
        _oa_core_path = str(Path(__file__).resolve().parent.parent / 'freepp-backend')
        if _oa_core_path not in sys.path:
            sys.path.insert(0, _oa_core_path)
        from core import oaics_proto as _op
        # 用与建单/update 相同的设备身份
        confirm_data = _op.confirm_oaics_standard_paypal(
            proxy, access_token, "", cs, entity, ctoken,
            country=country, device_id=_oa_device_id,
            sentinel_headers=None, attestation="",
        )
    except Exception as e:
        msg = str(e)
        if 'blocked' in msg.lower():
            raise PaymentLinkError(f"OpenAI confirm blocked: {msg[:150]}")
        raise PaymentLinkError(f"OpenAI confirm 失败: {msg[:250]}")
    client_secret = deep_first(confirm_data, ("client_secret", "payment_intent", "setup_intent"))
    if isinstance(client_secret, dict):
        client_secret = client_secret.get("client_secret") or ""
    client_secret = str(client_secret or "")
    if "_secret_" not in client_secret:
        raise PaymentLinkError(f"confirm 未返回 client_secret: {str(confirm_data)[:300]}")
    log("confirm", f"OpenAI confirm OK ({time.time() - t0:.1f}s)")

    # 7) Stripe intent confirm
    intent_id = client_secret.split("_secret_", 1)[0]
    if intent_id.startswith("pi_"):
        endpoint = f"https://api.stripe.com/v1/payment_intents/{intent_id}/confirm"
    elif intent_id.startswith("seti_"):
        endpoint = f"https://api.stripe.com/v1/setup_intents/{intent_id}/confirm"
    else:
        raise PaymentLinkError(f"client_secret 前缀异常: {intent_id[:16]}")
    return_url = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs}&processor_entity={entity}&plan_type=plus"
    intent_body = {
        "return_url": return_url,
        "confirmation_token": ctoken,
        "client_secret": client_secret,
        "key": pk,
        "_stripe_version": STRIPE_VERSION,
        "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
        "client_attribution_metadata[checkout_session_id]": cs,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
    }
    resp = stripe.post(endpoint, data=intent_body, timeout=timeout)
    if resp.status_code >= 400:
        raise PaymentLinkError(f"intent confirm 失败 HTTP {resp.status_code}: {str(resp.text)[:300]}")
    intent_payload = resp.json() or {}

    # 8) 提取 BA 链 (resolve pm-redirects, 对齐参考 opll_resolve_external_redirect)
    redirect = extract_redirect_url(intent_payload)

    def _resolve_ba(start_url: str, max_hops: int = 6) -> str:
        current = str(start_url or "").strip()
        for _ in range(max_hops):
            if not current:
                return ""
            if BA_RE.match(current):
                return current
            try:
                host = (urlsplit(current).netloc or "").lower()
            except Exception:
                host = ""
            if host == "paypal.com" or host.endswith(".paypal.com"):
                if "agreements/approve" in current:
                    return current
            # 依次尝试 curl session -> 标准 requests (pm-redirects 无需浏览器指纹)
            for client in (stripe, None):
                dbg = "curl" if client is not None else "requests"
                try:
                    if client is None:
                        r2 = requests.get(current, timeout=timeout, allow_redirects=False,
                                          proxies={"http": promo, "https": promo},
                                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"})
                    else:
                        r2 = client.get(current, timeout=timeout, allow_redirects=False)
                except Exception as exc:
                    log("resolve", f"[{dbg}] GET {current[:60]} EXC {str(exc)[:100]}")
                    continue
                log("resolve", f"[{dbg}] GET {current[:60]} -> {r2.status_code}")
                if r2.status_code in (301, 302, 303, 307, 308):
                    loc = str(r2.headers.get("Location") or "").strip()
                    log("resolve", f"[{dbg}] LOC {loc[:100]}")
                    if loc:
                        current = urljoin(current, loc)
                        break
                else:
                    body = str(getattr(r2, "text", "") or "")
                    m = BA_RE.search(body)
                    if m:
                        return m.group(0)
                    for m in PAYPAL_PAGE_RE.finditer(body):
                        return m.group(0)
                    return current
            else:
                return current
        return current

    resolved = _resolve_ba(redirect)
    if not BA_RE.match(resolved or ""):
        # 兜底: 从 confirm/intent 全量文本再找一次
        fallback = BA_RE.search(json.dumps({**confirm_data, "intent": intent_payload}, default=str))
        resolved = fallback.group(0) if fallback else ""
    if not resolved:
        raise PaymentLinkError(f"未提取到 BA 链: {redirect or str(intent_payload)[:300]}")
    log("done", f"[出口国={exit_label}] [建单国={country}] BA 链提取成功 ({time.time() - t0:.1f}s)")
    return {
        "ok": True,
        "method": "oa",
        "ba_url": resolved,
        "stripe_redirect_url": redirect,
        "checkout_session_type": "oaics",
        "billing_country": country,
        "currency": currency,
        "exit_country": exit_label,
        "exit_ip": exit_ip,
        "payment_methods": methods,
        "amount": amount,
        "checkout_session_id": cs,
    }


# ─── CS 链路 (预留, 对齐参考实现 cslive 分支) ───────────────────────────────

def extract_cs_ba_link(
    access_token: str,
    proxy: str,
    billing_country: str = "DE",
    promo_proxy: str = "",
    emit: Callable[[str, str], None] | None = None,
    timeout: float = 30.0,
) -> dict:
    """CS 链路占位: 结构对齐 OA, 待补 confirmation-token + approve 页面化方案。"""
    raise PaymentLinkError("CS 链路待接入: 需 kidders 型账号(促销区建单出paypal)或页面 approve 方案")


# ─── 统一入口 ────────────────────────────────────────────────────────────────

def extract_payment_link(
    access_token: str,
    proxy: str,
    billing_country: str = "US",
    promo_proxy: str = "",
    method: str = "oa",
    emit: Callable[[str, str], None] | None = None,
    timeout: float = 30.0,
) -> dict:
    """统一提链入口: method=oa (已验证) / cs (预留)。"""
    if str(method or "oa").strip().lower() in {"oa", "oaics", "openai"} :
        return extract_oa_ba_link(access_token, proxy, billing_country, promo_proxy, emit, timeout)
    return extract_cs_ba_link(access_token, proxy, billing_country, promo_proxy, emit, timeout)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="OA/CS 链路提链 (纯AT协议)")
    ap.add_argument("--at", required=True, help="access token")
    ap.add_argument("--proxy", required=True, help="主代理 (出口, 支持 host:port:user:pass 等格式)")
    ap.add_argument("--promo-proxy", default="", help="促销代理 (默认同主代理)")
    ap.add_argument("--country", default="US", help="建单国 (US/DE/GB/JP...)")
    ap.add_argument("--method", default="oa", help="链路: oa (已验证) / cs (预留)")
    ap.add_argument("--json", default="", help="结果 JSON 输出路径")
    args = ap.parse_args()
    try:
        result = extract_payment_link(args.at, args.proxy, args.country, args.promo_proxy, args.method)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.json:
            Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())