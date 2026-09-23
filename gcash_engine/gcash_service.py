#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GCash 提链服务 CLI —— 供 Node.js 提链控制台（server.js / extractor.js）调用。

完整 GCash 协议提链逻辑（取自 GPT-Register-Tool/sms_tool 钱包适配器）：
    建单(checkout) → 促销/压0(promotion) → Stripe init / checkout page（能力+金额校验）
    → 提取 GCash Checkout 授权链接(extract_link)

用法:
    python gcash_service.py --at <access_token> --proxy host:port:user:pass
        [--proxy-pool JSON数组]   # 用户写的整个代理池（轮换兜底）
        [--exit-proxy host:port:user:pass]              # 出口/账单侧代理（第二池，可空=同主池）
        [--country PH] [--currency PHP]
        [--promo-campaign-id plus-1-month-free]
        [--checkout-ui-mode custom|hosted]              # 默认 custom（oaics 会话）
        [--require-zero | --no-require-zero]            # 默认 require-zero（0 元才算成功）
        [--timeout 90] [--json ""]

过程日志（stdout 每行，供前端"像 PayPal 一样"逐步展示）:
    [stage] [status] msg        # status: run / ok / fail
最终输出（stdout 最后一行，单行 JSON）:
    {"ok": true, "link": "https://chatgpt.com/checkout/...", "extra": {...}}
    {"ok": false, "error": "...", "reason_code": "...", "extra": {...}}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path

# 引擎根目录 = 本文件所在目录（sms_tool 为其中的子包）
_ENGINE_ROOT = str(Path(__file__).resolve().parent)
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

from sms_tool.paypal_proxy import normalize_proxy_url, probe_proxy, redact_proxy_url  # noqa: E402
from sms_tool.wallet_provider import redact_sensitive_text, run_wallet_provider  # noqa: E402
from sms_tool.wallet_transport import ChatGPTStripeWalletTransport  # noqa: E402


# 二维码 Base64 行很大，且监控线程和主线程会同时写 stdout。
# 统一串行输出，避免事件 JSON 与最终结果 JSON 相互穿插后 Node 无法解析。
_STDOUT_LOCK = threading.Lock()


def _print_stdout(value: str) -> None:
    with _STDOUT_LOCK:
        print(value, flush=True)

# GCash 契约（与 checkout_contract.PAYMENT_METHOD_PROFILES["gcash"] 一致）
_COUNTRY_CURRENCY = {
    "PH": "PHP", "US": "USD", "GB": "GBP", "DE": "EUR", "JP": "JPY", "ID": "IDR",
    "VN": "VND", "TH": "THB", "IN": "INR", "BR": "BRL", "TR": "TRY", "MX": "MXN",
    "AR": "ARS", "CL": "CLP", "CO": "COP", "PE": "PEN", "SA": "SAR", "AE": "AED",
    "NG": "NGN", "ZA": "ZAR", "KR": "KRW", "MY": "MYR", "SG": "SGD", "AU": "AUD",
    "NZ": "NZD", "CA": "CAD", "HK": "HKD", "TW": "TWD",
}


def _emit(stage: str, status: str, msg: str, limit: int = 800) -> None:
    text = redact_sensitive_text(msg)
    if limit > 0 and len(text) > limit:
        text = text[:limit] + "..."
    _print_stdout(f"[{stage}] [{status}] {text}")


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
    "wallet_method_unavailable": "账号没有 {method} 通道资格：建单未提供该支付方式（可用: {types}）。系统已停止自动重试（{stage}）",
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
    "checkout_risk_blocked": "GCash 建单被上游异常活动风控拒绝（{stage}）。系统已停止自动重试并进入账号冷却；本次全程使用用户填写的同一代理，冷却结束后再提交",
    "promo_not_applied": "账号没有优惠资格：plus-1-month-free 促销未生效，系统已停止自动重试（{stage}）",
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
    home_cc: str = "PH",
    method_label: str = "GCash",
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
    fingerprint: dict | None = None,
    sticky_proxy: bool = False,
) -> dict:
    """构建钱包传输上下文：用户写什么代理池就用什么。

    - checkout_proxy: 主池当前轮询到的代理（建单）
    - side_proxy: 出口/账单侧代理（促销/支付方式等），为空则同主池
    - proxy_pool: 用户整个代理池，供各阶段在主代理失败时轮换兜底
    - sticky_proxy: 粘性代理开关 —— 开启后禁用池内轮换，IP 全程不变
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
        "proxy_pool": [] if sticky_proxy else pool,
        # 粘性代理：轮换候选上限 = 1（只试主代理），代理/出口 IP 全程不变
        "proxy_rotation_attempts": 1 if sticky_proxy else (max(1, min(len(pool) + 1, 10)) if pool else 1),
        "promotion_attempts": 3,
        "fingerprint": fingerprint or {},
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
    timeout: int,
    fingerprint: dict | None = None,
    sticky_proxy: bool = False,
    on_qr_update=None,
    qr_refresh_event=None,
    on_paid=None,
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

    # 账单国家：显式 > 代理出口国标记 > 默认 PH（用户写什么代理就用什么）
    cc = _resolve_billing_country(country, checkout_proxy, side_proxy, pool, "PH", _emit)
    if cc not in _COUNTRY_CURRENCY:
        return {
            "ok": False,
            "link": "",
            "error": f"账单国家 {cc} 不受支持（支持: {sorted(_COUNTRY_CURRENCY.keys())}）",
            "reason_code": "country_unsupported",
            "extra": {"payment_method": "gcash", "error_stage": "validation",
                      "retryable": False, "elapsed_seconds": round(time.monotonic() - t0, 1)},
        }
    cur = str(currency or _COUNTRY_CURRENCY[cc]).strip().upper()

    _emit("gcash", "run", "开始 GCash 协议提链（0元=" + ("是" if require_zero else "否") + "）")
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
    if fp:
        _emit("fingerprint", "run",
              "账号指纹 " + _fingerprint_summary(fp) + " 内核=" + str(fp.get("kernel") or "?")
              + " TLS=" + str(fp.get("impersonate") or "?")
              + " 语言=" + str(fp.get("oai_language") or "?") + " 时区=" + str(fp.get("timezone") or "?")
              + " 建单国=" + str(fp.get("country") or cc))

    if not at:
        return {
            "ok": False,
            "link": "",
            "error": "access_token 为空",
            "reason_code": "wallet_access_token_missing",
            "extra": {"payment_method": "gcash", "error_stage": "validation", "retryable": False},
        }

    tc = _build_transport_context(checkout_proxy, side_proxy, pool, cc, fingerprint=fp or None, sticky_proxy=sticky_proxy)
    transport = _StageLoggingTransport(
        ChatGPTStripeWalletTransport(timeout=max(5, int(timeout or 90)), fingerprint=fp or None), _emit
    )

    # 账号指纹的 device_id 注入 auth_context：传输层全程作为
    # oai-device-id / Cookie oai-did，建单→促销→approve→poll 身份一致
    auth_ctx = {}
    if fp and fp.get("device_id"):
        auth_ctx["device_id"] = str(fp["device_id"])

    # 模式分流：
    #   custom（默认）→ gcash_custom_flow：
    #     读取 PH 定价配置 → 按浏览器 HAR 建单（GCash 由响应 custom_payment_methods 返回）
    #     → elements → PaymentMethod → OpenAI confirm → custom_payment_method/start → 授权链接
    #   hosted → wallet_provider 钱包流程（cs_live_ 会话，历史路径）
    ui_mode = str(checkout_ui_mode or "custom").strip()
    if ui_mode.lower() == "custom":
        _emit("config", "run", "提链模式=custom 纯协议流程（目标: m.gcash.com 授权链接）")
        try:
            # 脚本模式（python gcash_service.py）下不能用相对导入；_ENGINE_ROOT 已在顶部注入 sys.path
            from gcash_custom_flow import extract_gcash_custom_link
        except Exception as exc:
            return {
                "ok": False, "link": "", "error": f"GCash custom 流程模块加载失败: {exc}",
                "reason_code": "engine_load_failed",
                "extra": {"payment_method": "gcash", "error_stage": "engine", "retryable": False,
                          "elapsed_seconds": round(time.monotonic() - t0, 1)},
            }
        result = extract_gcash_custom_link(
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
            on_qr_update=on_qr_update,
            qr_refresh_event=qr_refresh_event,
            on_paid=on_paid,
        )
        result.setdefault("extra", {})
        result["extra"].setdefault("checkout_ui_mode", "custom")
        if on_qr_update and result.get("ok") and result.get("qr_image"):
            result["extra"].setdefault("qr_refresh_active", True)
        if fp:
            result["extra"]["fingerprint"] = _fingerprint_summary(fp)
        return result
    _emit("config", "run", "提链模式=hosted 钱包流程（cs_live_ 会话）")
    result = run_wallet_provider(
        "gcash",
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
        checkout_ui_mode=ui_mode,
        require_zero=bool(require_zero),
        max_approve_attempts=6,
        max_poll_attempts=25,
        poll_interval_seconds=2.0,
    )

    capability = result.get("capability") if isinstance(result.get("capability"), dict) else {}
    elapsed = round(time.monotonic() - t0, 1)
    ok = bool(result.get("ok"))
    url = str(result.get("url") or result.get("provider_redirect_url") or result.get("checkout_url") or "")
    if ok and not url:
        ok = False

    link_type = str(result.get("link_type") or "")
    if not link_type and ok:
        link_type = "gcash_protocol" if "gcash.com" in url or "mynt.com" in url else "gcash_checkout_link"

    extra = {
        "payment_method": "gcash",
        "billing_country": cc,
        "link_type": link_type,
        "amount_minor": capability.get("amount_minor"),
        "currency": capability.get("currency") or cur,
        "offer_state": capability.get("offer_state") or "",
        "promotion_applied": result.get("promotion_applied"),
        "promotion_error": str(result.get("promotion_error") or ""),
        "checkout_session_type": str(result.get("checkout_session_type") or ""),
        "checkout_session_id_hint": str(result.get("checkout_session_id_hint") or ""),
        "checkout_ui_mode": str(result.get("checkout_ui_mode") or checkout_ui_mode),
        "error_stage": str(result.get("error_stage") or ""),
        "retryable": bool(result.get("retryable")),
        "elapsed_seconds": elapsed,
    }
    if isinstance(result.get("checkout_eligibility"), dict):
        extra["checkout_eligibility"] = result["checkout_eligibility"]
    if result.get("checkout_session_id"):
        extra["checkout_session_id"] = str(result["checkout_session_id"])

    if ok and url:
        _emit("resolve", "ok", ("GCash 钱包授权链接已提取（m.gcash.com）" if ("gcash.com" in url or "mynt.com" in url)
                                else "GCash Checkout 链接已提取") + "：" + str(url)[:110] + "…")
        return {
            "ok": True,
            "link": url,
            "error": "",
            "reason_code": "",
            "extra": extra,
        }

    raw_error_text = str(result.get("error") or "GCash 提链失败")
    reason_code = str(result.get("error_code") or "")
    stage = str(result.get("error_stage") or "resolve")
    promotion_error = str(result.get("promotion_error") or "")
    # 原始英文错误保留在 extra（排障用），展示字段全部中文化
    extra["error_raw"] = raw_error_text
    error_text = _translate_error(
        reason_code, raw_error_text, stage,
        capability=capability, cc=cc, home_cc="PH", method_label="GCash",
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

    # 非 PH 账单：建单通常不含 gcash 渠道，提前说明，避免用户误判
    if not ok and cc != "PH" and reason_code not in ("checkout_billing_mismatch",):
        _emit("config", "warn", f"建单国家={cc} 非 GCash 常用国家（菲律宾），上游可能不返回 gcash 渠道；"
                                f"请以实际出口和上游返回能力为准")

    _emit(stage, "fail", "GCash 提链失败：" + error_text + ((" [" + reason_code + "]") if reason_code else ""))
    return {
        "ok": False,
        "link": "",
        "error": error_text,
        "reason_code": reason_code,
        "extra": extra,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="GCash 提链服务 CLI")
    parser.add_argument("--at", required=True, help="access_token")
    parser.add_argument("--proxy", default="", help="建单代理 (host:port 或 host:port:user:pass)")
    parser.add_argument("--proxy-pool", default="", help="用户代理池 JSON 数组字符串")
    parser.add_argument("--exit-proxy", default="", help="出口/账单侧代理（可空=同主池）")
    parser.add_argument("--country", default="", help="建单国家（默认空=从代理自动解析出口国，解析不到回退 PH）")
    parser.add_argument("--currency", default="", help="货币（默认按国家，PH→PHP）")
    parser.add_argument("--promo-campaign-id", default="plus-1-month-free", help="促销活动 ID")
    parser.add_argument("--checkout-ui-mode", default="custom", help="checkout UI 模式（默认 custom=纯协议六步流程，产出 m.gcash.com 授权链接；hosted=钱包流程）")
    parser.add_argument("--require-zero", dest="require_zero", action="store_true", help="要求 0 元才算成功（默认开启，与 PayPal 一致；无优惠资格报 promo_not_applied）")
    parser.add_argument("--no-require-zero", dest="require_zero", action="store_false", help="关闭 0 元校验（原价链接）")
    parser.set_defaults(require_zero=True)
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

    def emit_qr_update(qr_image: str) -> None:
        try:
            _print_stdout(json.dumps({
                "event": "qr_refresh",
                "qr_image": str(qr_image or ""),
                "qr_refresh_active": True,
                "updated_at": time.time(),
            }, ensure_ascii=False, separators=(",", ":")))
        except BrokenPipeError:
            raise SystemExit(0)

    def emit_paid_update(status: str) -> None:
        try:
            _print_stdout(json.dumps({
                "event": "payment_status",
                "payment_status": str(status or ""),
                "paid": str(status or "") == "plus_active",
                "qr_refresh_active": False,
                "updated_at": time.time(),
            }, ensure_ascii=False, separators=(",", ":")))
        except BrokenPipeError:
            raise SystemExit(0)

    # Node 在同一子进程的 stdin 写入 refresh_qr，确保二维码页面继续复用
    # 当前浏览器上下文、Cookie、代理和指纹；没有固定刷新计时器。
    qr_refresh_event = threading.Event()

    def read_commands() -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                return
            try:
                command = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(command, dict) and command.get("command") == "refresh_qr":
                qr_refresh_event.set()

    try:
        threading.Thread(target=read_commands, name="gcash-qr-control", daemon=True).start()
    except RuntimeError as exc:
        # 控制线程仅负责手动刷新；资源紧张时不能因此让首次提链直接崩溃。
        _emit("config", "warn", "二维码控制线程启动失败，首次二维码仍会继续获取：" + str(exc)[:120])

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
        timeout=int(args.timeout or 90),
        fingerprint=_parse_fingerprint(args.fingerprint),
        sticky_proxy=str(args.sticky_proxy or "0").strip() in {"1", "true", "yes", "on"},
        on_qr_update=emit_qr_update,
        qr_refresh_event=qr_refresh_event,
        on_paid=emit_paid_update,
    )
    _print_stdout(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
