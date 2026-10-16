#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GoPay 提链服务 CLI —— 供 Node.js 提链控制台（server.js / extractor.js）调用。

完整 GoPay 协议提链逻辑（取自 GPT-Register-Tool/sms_tool 钱包适配器）：
    建单(checkout) → 促销/压0(promotion) → Stripe init → 支付方式(payment_method)
    → confirm → approve → poll → 提取 GoPay 授权链接(resolve)

用法:
    python gopay_service.py --at <access_token> --proxy host:port:user:pass
        [--proxy-pool '["host:port:user:pass", ...]']   # 用户写的整个代理池（轮换兜底）
        [--exit-proxy host:port:user:pass]              # 出口/账单侧代理（第二池，可空=同主池）
        [--country ID] [--currency IDR]
        [--promo-campaign-id plus-1-month-free]
        [--checkout-ui-mode hosted]
        [--require-zero | --no-require-zero]            # 默认 require-zero（0 元才算成功）
        [--gopay-browser-fallback 0|1]                  # 协议直取失败后是否开浏览器兜底（默认关）
        [--timeout 90] [--json ""]

过程日志（stdout 每行，供前端"像 PayPal 一样"逐步展示）:
    [stage] [status] msg        # status: run / ok / fail
最终输出（stdout 最后一行，单行 JSON）:
    {"ok": true, "link": "...", "extra": {...}}
    {"ok": false, "error": "...", "reason_code": "...", "extra": {...}}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# 引擎根目录 = 本文件所在目录（sms_tool 为其中的子包）
_ENGINE_ROOT = str(Path(__file__).resolve().parent)
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

from sms_tool.paypal_proxy import normalize_proxy_url, probe_proxy, redact_proxy_url  # noqa: E402
from sms_tool.wallet_provider import redact_sensitive_text, run_wallet_provider  # noqa: E402
from sms_tool.wallet_transport import ChatGPTStripeWalletTransport  # noqa: E402

# GoPay 授权链接主机（用于最终校验，与 wallet_provider.WALLET_METHODS 一致）
_GOPAY_HOST_MARKERS = (
    "pm-redirects.stripe.com",
    "app.midtrans.com",
    "gopay.co.id",
    "gojek.com",
    "checkout.stripe.com",
    "pay.openai.com",
)


def _emit(stage: str, status: str, msg: str, limit: int = 220) -> None:
    text = redact_sensitive_text(msg)
    if limit > 0 and len(text) > limit:
        text = text[:limit] + "..."
    print(f"[{stage}] [{status}] {text}", flush=True)


def _normalize_pool(lines) -> list[str]:
    """把用户代理池（任意行格式）规范化为 url 形式列表，去空去重。"""
    pool: list[str] = []
    seen: set[str] = set()
    for item in lines or []:
        value = normalize_proxy_url(str(item or "").strip())
        if value and value not in seen:
            seen.add(value)
            pool.append(value)
    return pool

# ── 账单国家自动解析（用户写什么代理就用什么）──────────────────────────
_COUNTRY_CURRENCY = {
    "ID": "IDR", "PH": "PHP", "US": "USD", "GB": "GBP", "DE": "EUR", "JP": "JPY",
    "VN": "VND", "TH": "THB", "IN": "INR", "BR": "BRL", "TR": "TRY", "MX": "MXN",
    "AR": "ARS", "CL": "CLP", "CO": "COP", "PE": "PEN", "SA": "SAR", "AE": "AED",
    "NG": "NGN", "ZA": "ZAR", "KR": "KRW", "MY": "MYR", "SG": "SGD", "AU": "AUD",
    "NZ": "NZD", "CA": "CAD", "HK": "HKD", "TW": "TWD",
}
_REGION_GATEWAYS = {"EU", "SE", "AP", "GLOBAL", "ALL"}  # 区域网关不算国家


def proxy_country_hint(proxy: str) -> str:
    """从代理串解析出口国家标记，解析不到返回 ''。
    
    优先级:
      1. region-XX / country-XX / cc-XX / geo-XX 显式标记（辣椒格式）
      2. 用户名中 -XX- 独立标记（kookeey 格式 user-...-XX-sid-...）
      3. gate-xx 国家网关域名（kookeey gate-gb / gate-jp 等；忽略 gate-eu / gate-sea 区域网关）
    """
    raw = str(proxy or "")
    if not raw:
        return ""
    m = re.search(r"(?i)(?:region|country|cc|geo)[-_=:/]+([a-z]{2})(?=$|[^a-z0-9])", raw)
    if m:
        cc = m.group(1).upper()
        if cc not in _REGION_GATEWAYS and cc in _COUNTRY_CURRENCY:
            return cc
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Z]{2})(?=[-_@:/]|$)", raw):
        cc = m.group(1).upper()
        if cc in _COUNTRY_CURRENCY and cc not in _REGION_GATEWAYS:
            return cc
    m = re.search(r"(?i)gate-([a-z]{2})\.[a-z0-9.-]+", raw)
    if m:
        cc = m.group(1).upper()
        if cc in _COUNTRY_CURRENCY and cc not in _REGION_GATEWAYS:
            return cc
    return ""


def _resolve_billing_country(
    explicit: str,
    checkout_proxy: str,
    side_proxy: str,
    pool: list[str],
    default: str,
    emit,
) -> str:
    """确定账单国家：显式指定 > 代理串标记 > 默认值。

    Web 主流程始终显式传入 country。代理串推断仅保留给旧 CLI/兼容调用，
    且只用于补全账单字段，不会改写任何代理 URL。
    """
    explicit_cc = str(explicit or "").strip().upper()
    if explicit_cc:
        return explicit_cc
    hint = (
        proxy_country_hint(checkout_proxy)
        or proxy_country_hint(side_proxy)
        or next((proxy_country_hint(p) for p in pool if proxy_country_hint(p)), "")
    )
    if hint:
        emit("config", "run", f"未指定 country，已从代理自动解析出口国：{hint}（账单国=代理出口国）")
        return hint
    emit("config", "warn", "未指定 country 且代理无国家标记，使用默认账单国 " + str(default) +
           "。如遇 Billing country mismatch，请在附加参数指定与代理出口一致的 country")
    return default


def _probe_exit_country(proxy: str) -> str:
    """用代理探测实际出口国家（ipwho.is / ip-api），失败返回 ''。耗时约 2-6s。"""
    if not proxy:
        return ""
    try:
        result = probe_proxy(proxy, timeout=6)
        return str(getattr(result, "country_code", "") or "").upper()
    except Exception:
        return ""

# ── 错误中文化（reason_code / 原始错误 → 中文说明）────────────────────
_PM_TYPE_LABELS = {
    "card": "银行卡", "paypal": "PayPal", "gcash": "GCash", "gopay": "GoPay",
    "grabpay": "GrabPay", "momo": "MoMo", "link": "银行卡/账户", "ideal": "iDEAL",
    "upi": "UPI", "pix": "PIX", "kakao_pay": "Kakao Pay", "blik": "BLIK", "twint": "TWINT",
}
_STAGE_CN = {
    "validation": "参数校验", "checkout": "建单", "promotion": "促销压0",
    "stripe_init": "Stripe 初始化", "checkout_page": "账单页校验", "payment_method": "创建支付方式",
    "confirm": "确认支付", "approve": "审核", "poll": "轮询结果", "extract_link": "提取链接",
    "follow_redirect": "跳转跟进", "resolve": "提取链接", "engine": "引擎",
    "checkout_response": "建单响应", "checkout_contract": "参数构造", "adapter_subprocess": "子进程",
}
_ERROR_CODE_CN = {
    "wallet_access_token_missing": "access_token 为空，无法提链（{stage}）",
    "access_token_invalid_or_expired": "access_token 无效或已过期（HTTP 401），请更换凭证（{stage}）",
    "checkout_cloudflare_blocked": "请求被 Cloudflare 拦截（HTTP 403），代理 IP 可能被风控，请更换代理重试（{stage}）",
    "wallet_method_unavailable": "建单未提供 {method} 支付方式（可用: {types}）。当前建单国家={cc}；上游渠道能力可能受建单国家与实际出口影响，请检查代理实际出口和国家选择（{stage}）",
    "wallet_capability_unknown": "建单成功但无法确认支付方式列表（{stage}），可重试或切换 checkout-ui-mode 再试",
    "wallet_checkout_not_zero_due": "促销压0未生效：当前金额为 {amount}，账号无优惠资格或促销未正确应用（{stage}）。请换有优惠资格的账号，或关闭 require-zero 输出原价链接",
    "wallet_checkout_link_missing": "建单响应缺少可用的 Checkout 链接（{stage}）",
    "checkout_session_missing": "建单响应缺少会话 ID（{stage}）",
    "checkout_publishable_key_missing": "建单响应缺少 Stripe 公钥（{stage}）",
    "checkout_bad_response": "建单响应格式异常（{stage}），请重试或更换代理",
    "stripe_init_bad_response": "Stripe 初始化响应异常（{stage}），请重试或更换代理",
    "checkout_transport_failed": "建单网络请求失败（{stage}），请检查代理连通性后重试",
    "promotion_transport_failed": "促销更新网络请求失败（{stage}），请检查代理连通性后重试",
    "checkout_rate_limited": "建单被限流（HTTP 429），请等待 30-60 秒后重试（{stage}）",
    "checkout_failed": "建单失败（{stage}），请更换代理后重试",
    "promotion_not_applied": "促销未生效（{stage}），账号可能无优惠资格",
    "promotion_rejected": "促销被拒绝（{stage}），账号可能无优惠资格",
    "invalid_promotion": "促销无效或已被消耗（{stage}），请更换账号",
    "extractor_timed_out": "引擎执行超时（{stage}），请重试",
    "payment_link_cancelled": "提链任务已取消",
}


def _pm_labels(types) -> str:
    """支付方式列表 → 中文标签。"""
    if not types:
        return "（空）"
    labels = [str(_PM_TYPE_LABELS.get(str(t), str(t))) for t in types]
    return "、".join(labels)


def _translate_error(
    reason_code: str,
    raw_error: str,
    stage: str,
    capability: dict | None = None,
    cc: str = "",
    home_cc: str = "ID",
    method_label: str = "GoPay",
) -> str:
    """把引擎返回的错误翻译成中文说明；原始英文保留在 extra.error_raw。
    
    优先级: reason_code 映射 > 网络/HTTP 特征识别 > 兜底（已是中文则原样保留）。
    """
    raw = str(raw_error or "")
    low = raw.lower()
    stage_cn = str(_STAGE_CN.get(stage, stage) or "未知阶段")
    # 已由上层生成的中文错误（billing mismatch / country 不支持）直接保留
    if reason_code in ("checkout_billing_mismatch", "country_unsupported"):
        return raw
    tpl = _ERROR_CODE_CN.get(reason_code)
    if tpl:
        msg = tpl.replace("{stage}", stage_cn)
        msg = msg.replace("{method}", method_label).replace("{home}", home_cc).replace("{cc}", cc or home_cc)
        if "{types}" in msg:
            types = (capability or {}).get("payment_method_types") or []
            msg = msg.replace("{types}", _pm_labels(types))
        if "{amount}" in msg:
            amt = (capability or {}).get("amount_minor")
            cur = (capability or {}).get("currency") or ""
            msg = msg.replace("{amount}", (str(amt) + " " + str(cur)) if amt is not None else "未知")
        return msg
    # 网络 / HTTP 特征识别（无 reason_code 或未知码时）
    if any(k in low for k in ("connectionerror", "connection refused", "connection reset", "remote end closed", "unexpected_eof")):
        return f"代理连接失败（{stage_cn}）：{raw}"
    if any(k in low for k in ("proxyerror", "unable to connect to proxy", "proxy gateway", "failed to establish a new connection")):
        return f"代理网关错误或拒绝连接（{stage_cn}）：{raw}"
    if any(k in low for k in ("timeout", "timed out")):
        return f"代理请求超时（{stage_cn}）：{raw}"
    if "ssl" in low:
        return f"SSL 握手失败（{stage_cn}）：{raw}"
    if "cloudflare" in low or "403" in low:
        return f"请求被 Cloudflare 拦截（HTTP 403），代理 IP 可能被风控（{stage_cn}）：{raw}"
    if "429" in low or "rate limit" in low:
        return f"请求被限流（HTTP 429），请等待 30-60 秒后重试（{stage_cn}）：{raw}"
    if "401" in low or "unauthorized" in low or "invalid or expired" in low:
        return f"access_token 无效或已过期（HTTP 401），请更换凭证（{stage_cn}）：{raw}"
    if "400" in low:
        return f"建单请求被拒绝（HTTP 400）（{stage_cn}）：{raw}"
    # 兜底：原文含中文则原样保留，否则包一层中文说明
    if re.search("[\u4e00-\u9fff]", raw):
        return raw
    return f"提链失败（{method_label}，{stage_cn}）：{raw}"


class _StageLoggingTransport:
    """包装 ChatGPTStripeWalletTransport：每个协议步骤前后打日志，不改原逻辑。"""

    def __init__(self, inner, emit) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_emit", emit)

    def __getattr__(self, name: str):
        attr = getattr(object.__getattribute__(self, "_inner"), name)
        if name.startswith("_") or not callable(attr):
            return attr

        def wrapper(request=None, *args, **kwargs):
            stage = str(getattr(request, "stage", None) or name)
            attempt = int(getattr(request, "attempt", None) or 1)
            self._emit(stage, "run", f"[{stage}] 请求开始（尝试 #{attempt}）")
            started = time.monotonic()
            try:
                result = attr(request, *args, **kwargs)
                elapsed = round(time.monotonic() - started, 1)
                self._emit(stage, "ok", f"[{stage}] 完成（{elapsed}s）")
                return result
            except Exception as exc:
                elapsed = round(time.monotonic() - started, 1)
                self._emit(stage, "fail", f"[{stage}] 失败（{elapsed}s）：{exc}")
                raise

        return wrapper


def _parse_fingerprint(raw: str) -> dict:
    """解析 --fingerprint JSON（账号指纹档案）。非法/空返回空 dict（引擎用内置默认指纹）。"""
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return dict(value) if isinstance(value, dict) else {}
    except Exception:
        _emit("fingerprint", "fail", "--fingerprint 不是合法 JSON，回退内置默认指纹")
        return {}


def _fingerprint_summary(fp: dict) -> str:
    """指纹摘要（展示用，不含敏感信息）。"""
    kernel = str(fp.get("kernel_label") or fp.get("kernel") or "chrome")
    device = str(fp.get("device_id") or "")[:8]
    return f"{kernel}/{device}"


def _build_transport_context(
    checkout_proxy: str,
    side_proxy: str,
    pool: list[str],
    cc: str,
    checkout_ui_mode: str,
    gopay_browser_fallback: bool,
    promotion_attempts: int,
    fingerprint: dict | None = None,
    sticky_proxy: bool = False,
) -> dict:
    """构建钱包传输上下文：用户写什么代理池就用什么。

    - checkout_proxy: 主池当前轮询到的代理（建单）
    - side_proxy: 出口/账单侧代理（促销/支付方式/approve 等），为空则同主池
    - proxy_pool: 用户整个代理池，供各阶段在主代理失败时轮换兜底
    - sticky_proxy: 粘性代理开关 —— 开启后禁用池内轮换，整个提链过程
      固定使用 checkout_proxy/side_proxy（代理与出口 IP 全程不变）
    """
    return {
        "checkout_proxy": checkout_proxy,
        "promotion_proxy": side_proxy,
        "provider_proxy": side_proxy,
        "approve_proxy": side_proxy,
        "stripe_init_proxy": side_proxy,
        "payment_method_proxy": side_proxy,
        "confirm_proxy": side_proxy,
        "redirect_proxy": side_proxy,
        "proxy_pool": pool,
        "proxy_rotation_attempts": max(1, min(len(pool) + 1, 10)) if pool else 1,
        "gopay_browser_fallback": bool(gopay_browser_fallback),
        "gopay_create_with_promo": True,
        "gopay_promotion_update": False,
        "promotion_attempts": max(1, min(int(promotion_attempts or 3), 5)),
        "fingerprint": fingerprint or {},
        # 粘性代理：轮换候选上限 = 1（只试主代理），代理/出口 IP 全程不变
        "proxy_rotation_attempts": 1 if sticky_proxy else max(1, min(len(pool) + 1, 10)) if pool else 1,
        "proxy_pool": [] if sticky_proxy else pool,
    }


def _run(
    at: str,
    proxy: str,
    proxy_pool: list[str],
    exit_proxy: str,
    country: str,
    currency: str,
    promo_campaign_id: str,
    checkout_ui_mode: str,
    require_zero: bool,
    gopay_browser_fallback: bool,
    timeout: int,
    fingerprint: dict | None = None,
    sticky_proxy: bool = False,
    gopay_cpmt_id: str = "",
    force_payment_method: bool = False,
    force_zero_checkout: bool = False,
) -> dict:
    t0 = time.monotonic()

    checkout_proxy = normalize_proxy_url(proxy)
    side_proxy = normalize_proxy_url(exit_proxy) or checkout_proxy
    pool = _normalize_pool(proxy_pool)
    # 粘性代理：整个提链过程固定主代理（不补池、不轮换），IP 不变
    if not sticky_proxy:
        # 主代理必须在池里（若用户未把当前代理写进池，则补进去，保证轮换可用）
        if checkout_proxy and checkout_proxy not in pool:
            pool.insert(0, checkout_proxy)

    # 账单国家：显式 > 代理出口国标记 > 默认 ID（用户写什么代理就用什么）
    cc = _resolve_billing_country(country, checkout_proxy, side_proxy, pool, "ID", _emit)
    if cc not in _COUNTRY_CURRENCY:
        return {
            "ok": False,
            "error": f"账单国家 {cc} 不受支持（支持: {sorted(_COUNTRY_CURRENCY.keys())}）",
            "reason_code": "country_unsupported",
            "extra": {"payment_method": "gopay", "error_stage": "validation",
                      "retryable": False, "elapsed_seconds": round(time.monotonic() - t0, 1)},
        }
    cur = str(currency or _COUNTRY_CURRENCY[cc]).strip().upper()

    _emit("gopay", "run", "开始 GoPay 协议提链（0元=" + ("是" if require_zero else "否") + "）")
    _emit(
        "config",
        "run",
        "建单代理=" + (redact_proxy_url(checkout_proxy) if checkout_proxy else "直连")
        + " 出口代理=" + (redact_proxy_url(side_proxy) if side_proxy else "同主池")
        + " 代理池=" + str(len(pool)) + " 条 国家=" + cc + "/" + cur,
    )
    _emit("config", "run", "促销活动=" + str(promo_campaign_id or "(无)") + " UI=" + str(checkout_ui_mode))
    if sticky_proxy:
        _emit("config", "run", "粘性代理已启用：建单/出口 IP 全程固定（不轮换）")
    fp = fingerprint or {}
    # 建单前一次性探测代理出口：城市用于同城匹配（id_billing._pick_by_city），
    # 经纬度供账单坐标反查使用。两者都写回 fp，同一账号/代理重试时不再重复探测。
    # 探测失败静默降级（账单库全随机），不阻断提链。
    if fp and not fp.get("proxy_city") and checkout_proxy:
        try:
            from id_billing import probe_exit_geo as _probe_geo
            _geo = _probe_geo(checkout_proxy, timeout=min(max(float(timeout or 90), 5.0), 8.0))
        except Exception:
            _geo = {}
        if _geo:
            fp = {**fp,
                  "proxy_city": str(_geo.get("city") or ""),
                  "proxy_country": str(_geo.get("country") or ""),
                  "proxy_latitude": str(_geo.get("latitude") or ""),
                  "proxy_longitude": str(_geo.get("longitude") or "")}
            _emit("config", "run",
                  "代理出口探测=" + str(_geo.get("city") or "?") + "/" + str(_geo.get("country") or "?")
                  + "（账单同城匹配 + 坐标反查）")
    if fp:
        _emit("fingerprint", "run",
              "账号指纹 " + _fingerprint_summary(fp) + " 内核=" + str(fp.get("kernel") or "?")
              + " TLS=" + str(fp.get("impersonate") or "?")
              + " 语言=" + str(fp.get("oai_language") or "?") + " 时区=" + str(fp.get("timezone") or "?")
              + " 建单国=" + str(fp.get("country") or cc))

    if not at:
        return {
            "ok": False,
            "error": "access_token 为空",
            "reason_code": "wallet_access_token_missing",
            "extra": {"payment_method": "gopay", "error_stage": "validation", "retryable": False},
        }

    tc = _build_transport_context(
        checkout_proxy,
        side_proxy,
        pool,
        cc,
        checkout_ui_mode,
        gopay_browser_fallback,
        3,
        fingerprint=fp or None,
        sticky_proxy=sticky_proxy,
    )

    transport = _StageLoggingTransport(
        ChatGPTStripeWalletTransport(timeout=max(5, int(timeout or 90)), fingerprint=fp or None), _emit
    )

    # 账号指纹的 device_id 注入 auth_context：传输层全程作为
    # oai-device-id / Cookie oai-did，建单→促销→approve→poll 身份一致
    auth_ctx = {}
    if fp and fp.get("device_id"):
        auth_ctx["device_id"] = str(fp["device_id"])

    # 模式分流：
    #   custom（默认）→ gopay_custom_flow：纯协议六步流程
    #     pricing_config → checkout(+风控) → taxes → confirm → cpmt/start → follow
    #     → Midtrans / pm-redirects 授权链接（对齐 GCash custom flow）
    #   hosted → wallet_provider 钱包流程（cs_live_ 会话，历史路径）
    ui_mode = str(checkout_ui_mode or "custom").strip()
    if ui_mode.lower() == "custom":
        _emit("config", "run", "提链模式=custom 纯协议流程（目标: Midtrans 授权链接）")
        try:
            from gopay_custom_flow import extract_gopay_custom_link
        except Exception as exc:
            return {
                "ok": False, "link": "", "error": f"GoPay custom 流程模块加载失败: {exc}",
                "reason_code": "engine_load_failed",
                "extra": {"payment_method": "gopay", "error_stage": "engine", "retryable": False,
                          "elapsed_seconds": round(time.monotonic() - t0, 1)},
            }
        result = extract_gopay_custom_link(
            str(at or "").strip(),
            checkout_proxy or (pool[0] if pool else ""),
            country=cc,
            currency=cur,
            promo_campaign_id=str(promo_campaign_id or "").strip(),
            require_zero=bool(require_zero),
            emit=_emit,
            timeout=max(10, int(timeout or 45)),
            fingerprint=fp or None,
            sticky_proxy=sticky_proxy,
            gopay_cpmt_id=str(gopay_cpmt_id or "").strip(),
            force_payment_method=bool(force_payment_method),
            force_zero_checkout=bool(force_zero_checkout),
        )
        result.setdefault("extra", {})
        result["extra"].setdefault("checkout_ui_mode", "custom")
        if fp:
            result["extra"]["fingerprint"] = _fingerprint_summary(fp)
        return result
    _emit("config", "run", "提链模式=hosted 钱包流程（cs_live_ 会话）")
    result = run_wallet_provider(
        "gopay",
        str(at or "").strip(),
        transport,
        link_only=True,
        auth_context=auth_ctx,
        transport_context=tc,
        browser_locale=str(fp.get("locale") or ""),
        browser_timezone=str(fp.get("timezone") or ""),
        checkout_country=cc,
        currency=cur,
        promo_campaign_id=str(promo_campaign_id or "").strip(),
        checkout_ui_mode=str(checkout_ui_mode or "hosted").strip(),
        require_zero=bool(require_zero),
        max_approve_attempts=6,
        max_poll_attempts=25,
        poll_interval_seconds=2.0,
    )

    capability = result.get("capability") if isinstance(result.get("capability"), dict) else {}
    elapsed = round(time.monotonic() - t0, 1)
    ok = bool(result.get("ok"))
    url = str(result.get("url") or "")
    extra = {
        "payment_method": "gopay",
        "billing_country": cc,
        "fingerprint": _fingerprint_summary(fp) if fp else "",
        "link_type": str(result.get("link_type") or ""),
        "amount_minor": capability.get("amount_minor"),
        "currency": capability.get("currency") or cur,
        "offer_state": capability.get("offer_state") or "",
        "promotion_applied": result.get("promotion_applied"),
        "promotion_error": str(result.get("promotion_error") or ""),
        "checkout_session_type": str(result.get("checkout_session_type") or ""),
        "checkout_session_id_hint": str(result.get("checkout_session_id_hint") or ""),
        "checkout_ui_mode": str(result.get("checkout_ui_mode") or ""),
        "error_stage": str(result.get("error_stage") or ""),
        "retryable": bool(result.get("retryable")),
        "elapsed_seconds": elapsed,
    }
    if isinstance(result.get("checkout_eligibility"), dict):
        extra["checkout_eligibility"] = result["checkout_eligibility"]

    if ok and url:
        _emit("resolve", "ok", "GoPay 授权链接已提取：" + str(url)[:110] + "…")
        return {
            "ok": True,
            "link": url,
            "error": "",
            "reason_code": "",
            "extra": extra,
        }

    raw_error_text = str(result.get("error") or "GoPay 提链失败")
    reason_code = str(result.get("error_code") or "")
    stage = str(result.get("error_stage") or "resolve")
    promotion_error = str(result.get("promotion_error") or "")
    # 原始英文错误保留在 extra（排障用），展示字段全部中文化
    extra["error_raw"] = raw_error_text
    error_text = _translate_error(
        reason_code, raw_error_text, stage,
        capability=capability, cc=cc, home_cc="ID", method_label="GoPay",
    )
    if promotion_error and reason_code in ("", "wallet_transport_error", "checkout_transport_failed"):
        error_text += "（促销:" + str(promotion_error) + "）"

    # ── 账单国≠代理出口国（ChatGPT HTTP 400）→ 明确中文原因 + 定位代理实际出口 ──
    if "Billing country must match request country" in raw_error_text or "must match request country" in raw_error_text.lower():
        reason_code = "checkout_billing_mismatch"
        hint = proxy_country_hint(checkout_proxy) or proxy_country_hint(side_proxy)
        _emit("config", "run", "检测到账单国与出口国不匹配，正在探测代理实际出口国家…")
        actual = _probe_exit_country(checkout_proxy or (pool[0] if pool else ""))
        parts = [f"上游拒绝建单：建单国家({cc})与代理实际出口不一致"]
        if actual:
            parts.append(f"代理实际出口={actual}")
        elif hint:
            parts.append(f"代理标记出口={hint}")
        parts.append("程序未修改代理；请确认代理实际出口，或选择与业务要求相符的建单国家")
        parts.append("如上游要求国家一致，请使用对应实际出口代理")
        error_text = "；".join(parts)
        extra["billing_country"] = cc
        extra["actual_exit_country"] = actual or hint or ""
        extra["country_auto"] = True

    # 非 ID 账单：建单通常不含 gopay 渠道，提前说明，避免用户误判
    if not ok and cc != "ID" and reason_code not in ("checkout_billing_mismatch",):
        _emit("config", "warn", f"建单国家={cc} 非 GoPay 常用国家（印尼），上游可能不返回 gopay 渠道；"
                                f"请以实际出口和上游返回能力为准")

    _emit(stage, "fail", "GoPay 提链失败：" + error_text + ((" [" + reason_code + "]") if reason_code else ""))
    return {
        "ok": False,
        "link": "",
        "error": error_text,
        "reason_code": reason_code,
        "extra": extra,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="GoPay 提链服务 CLI")
    parser.add_argument("--at", required=True, help="access_token")
    parser.add_argument("--proxy", default="", help="建单代理 (host:port 或 host:port:user:pass)")
    parser.add_argument("--proxy-pool", default="", help="用户代理池 JSON 数组字符串")
    parser.add_argument("--exit-proxy", default="", help="出口/账单侧代理（可空=同主池）")
    parser.add_argument("--country", default="", help="建单国家（默认空=从代理自动解析出口国，解析不到回退 ID）")
    parser.add_argument("--currency", default="", help="货币（默认空=按账单国自动推断，ID→IDR / TH→THB 等）")
    parser.add_argument("--promo-campaign-id", default="plus-1-month-free", help="促销活动 ID")
    parser.add_argument("--checkout-ui-mode", default="custom", help="checkout UI 模式（custom=纯协议六步流程，产出 Midtrans 授权链接；hosted=钱包流程）")
    parser.add_argument("--gopay-cpmt-id", default="", help="GoPay custom payment method type id（cpmt_...，可空=从建单响应动态提取；环境变量 MIN_GOPAY_CPMT_ID 同效）")
    parser.add_argument("--require-zero", dest="require_zero", action="store_true", help="要求 0 元才算成功")
    parser.add_argument("--no-require-zero", dest="require_zero", action="store_false", help="不校验 0 元（原价链接）")
    parser.set_defaults(require_zero=True)
    parser.add_argument("--force-payment-method", action="store_true", help="payment_method_types 无 gopay 时仍强制提交 gopay 支付方式")
    parser.add_argument("--force-zero", action="store_true", help="未确认 0 元时仍继续 confirm/start（强压 0 元）")
    parser.add_argument("--gopay-browser-fallback", default="0", help="协议失败后是否用浏览器兜底 (0/1，默认 0)")
    parser.add_argument("--timeout", default="90", help="单请求超时秒数")
    parser.add_argument("--fingerprint", default="", help="账号指纹档案 JSON（device_id/ua/impersonate/语言/时区，按建单国家分配）")
    parser.add_argument("--sticky-proxy", default="0", help="粘性代理 (0/1)：整个提链过程固定建单/出口代理，IP 不变")
    parser.add_argument("--json", default="", help="(兼容参数，忽略)")
    args = parser.parse_args()

    pool = []
    if str(args.proxy_pool or "").strip():
        try:
            parsed = json.loads(str(args.proxy_pool).strip())
            if isinstance(parsed, list):
                pool = [str(item) for item in parsed]
        except json.JSONDecodeError:
            _emit("config", "fail", "--proxy-pool 不是合法 JSON 数组，忽略代理池轮换")
            pool = []

    result = _run(
        at=args.at,
        proxy=args.proxy,
        proxy_pool=pool,
        exit_proxy=args.exit_proxy,
        country=args.country,
        currency=args.currency,
        promo_campaign_id=args.promo_campaign_id,
        checkout_ui_mode=args.checkout_ui_mode,
        require_zero=args.require_zero,
        gopay_browser_fallback=str(args.gopay_browser_fallback or "0").strip() in {"1", "true", "yes", "on"},
        timeout=int(args.timeout or 90),
        fingerprint=_parse_fingerprint(args.fingerprint),
        sticky_proxy=str(args.sticky_proxy or "0").strip() in {"1", "true", "yes", "on"},
        gopay_cpmt_id=args.gopay_cpmt_id,
        force_payment_method=bool(args.force_payment_method),
        force_zero_checkout=bool(args.force_zero),
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
