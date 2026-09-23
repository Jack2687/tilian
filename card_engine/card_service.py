#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直卡提链服务 CLI —— 供 Node.js 提链控制台（server.js / extractor.js）调用。

完整直卡协议提链逻辑（对齐 GPT-Register-Tool 源项目）：
    建单(checkout + promo_campaign) → 金额校验 → 输出 Checkout 长链

配方（源项目直卡成功记录，全部可参数覆盖）:
    billing_country=PH / PHP（账单国）
    checkout_proxy_country=空（默认不改写用户代理）
    update_proxy_country=空（默认不改写用户代理）

用法:
    python card_service.py --at <access_token> [--proxy host:port:user:pass]
        [--exit-proxy host:port:user:pass]                  # 促销更新代理（可空=同建单代理）
        [--proxy-pool '["host:port:user:pass", ...]']        # 用户写的整个代理池（轮换兜底）
        [--exit-pool '["...", ...]']                         # 出口/促销更新代理池（可空=复用主池）
        [--country PH] [--currency PHP]
        [--checkout-proxy-country TR] [--update-proxy-country TR]
        [--checkout-ui-mode hosted|custom]
        [--skip-proxy-check]                                 # 代理池模式自动跳过出口校验
        [--timeout 45] [--json ""]

过程日志（stdout 每行）:
    [stage] [status] msg        # status: run / ok / fail
最终输出（stdout 最后一行，单行 JSON）:
    {"ok": true, "link": "https://chatgpt.com/checkout/...", "extra": {...}}
    {"ok": false, "error": "<中文原因>", "reason_code": "<错误码>", "extra": {...}}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

# 引擎根目录 = 本文件所在目录（direct_card_extract.py 为同级自包含脚本）
_ENGINE_ROOT = str(Path(__file__).resolve().parent)
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

from direct_card_extract import (  # noqa: E402
    CheckoutExtractor,
    Credentials,
    ExtractorConfig,
    ExtractorError,
    InvalidPromotionError,
    NonZeroAmountError,
    UpstreamError,
    normalize_proxy_url,
    rotate_proxy_session,
)


def _emit(stage: str, status: str, msg: str, limit: int = 220) -> None:
    print(f"[{stage}] [{status}] {str(msg)[:limit]}", flush=True)


# ── 错误中文化（与 gcash_engine / gopay_engine 同款）──────────────────────
_STAGE_CN = {
    "validation": "参数校验", "preflight": "代理出口校验", "checkout": "建单",
    "promotion": "促销压0", "update": "促销压0", "verify": "金额校验",
    "extract": "提取链接", "engine": "引擎", "proxy": "代理",
}
_ERROR_CN = {
    "NonZeroAmountError": "促销压0未生效：账号无优惠资格或促销未正确应用，直卡输出非0元链接。请换有优惠资格的账号（或将附加参数 requireZero 设为 false）",
    "InvalidPromotionError": "促销无效或已被消耗（invalid_promotion），请更换账号",
    "UpstreamError": "上游接口错误（{stage}）：{raw}",
    "ExtractorError": "直卡引擎错误（{stage}）：{raw}",
    "Cancelled": "任务已取消",
}


def _translate_error(error_type: str, raw: str, stage: str) -> str:
    """错误类型/特征 → 中文说明。"""
    raw = str(raw or "")
    low = raw.lower()
    stage_cn = str(_STAGE_CN.get(stage, stage) or "未知阶段")
    # 特定业务错误优先模板（压0/促销/取消）
    if error_type in ("NonZeroAmountError", "InvalidPromotionError", "Cancelled"):
        return _ERROR_CN[error_type].replace("{stage}", stage_cn).replace("{raw}", raw)
    # 特征识别（网络/HTTP/代理出口）优先于通用 ExtractorError 模板
    if "billing country must match request country" in low:
        return (
            f"上游拒绝建单（{stage_cn}）：账单国家与请求国家不一致。"
            "国家选择只代表建单/账单国家，程序不会自动修改代理出口；"
            "请检查所填代理池的实际出口和上游支持的账单国家。"
        )
    if "proxy country mismatch" in low:
        return f"代理出口国家与显式配置的期望不符（{stage_cn}）：{raw}，请更换对应代理或清空代理国家配置"
    if "cloudflare" in low or "403" in low:
        return f"请求被 Cloudflare 拦截（HTTP 403），代理 IP 可能被风控（{stage_cn}）：{raw}"
    if "curl: (7)" in low or "failed to connect" in low or "could not connect" in low:
        return f"代理连接失败（{stage_cn}）：{raw}"
    if "curl: (28)" in low or "operation timed out" in low:
        return f"代理请求超时（{stage_cn}）：{raw}"
    if "curl: (35)" in low or "ssl" in low:
        return f"SSL 握手失败（{stage_cn}）：{raw}"
    if "curl: (56)" in low or "connection reset" in low or "recv failure" in low:
        return f"代理连接被重置（{stage_cn}）：{raw}"
    if any(k in low for k in ("connectionerror", "connection refused", "connection reset", "remote end closed")):
        return f"代理连接失败（{stage_cn}）：{raw}"
    if any(k in low for k in ("proxyerror", "unable to connect to proxy", "proxy gateway")):
        return f"代理网关错误或拒绝连接（{stage_cn}）：{raw}"
    if any(k in low for k in ("timeout", "timed out")):
        return f"代理请求超时（{stage_cn}）：{raw}"
    if "ssl" in low:
        return f"SSL 握手失败（{stage_cn}）：{raw}"
    if "429" in low or "rate limit" in low:
        return f"请求被限流（HTTP 429），请等待 30-60 秒后重试（{stage_cn}）：{raw}"
    if "401" in low or "unauthorized" in low or "invalid or expired" in low:
        return f"access_token 无效或已过期（HTTP 401），请更换凭证（{stage_cn}）：{raw}"
    if "already paid" in low:
        return f"账号已是付费用户，无法创建 Plus checkout（{stage_cn}）：{raw}"
    # 通用错误模板（ExtractorError/UpstreamError 等）
    tpl = _ERROR_CN.get(error_type or "")
    if tpl:
        return tpl.replace("{stage}", stage_cn).replace("{raw}", raw)
    # 兜底：含中文则保留
    if re.search("[\u4e00-\u9fff]", raw):
        return raw
    return f"直卡提链失败（{stage_cn}）：{raw}"


def _proxy_country_hint(proxy: str) -> str:
    """从代理串解析出口国家标记（region-XX / country-XX / cc-XX / geo-XX / gate-xx / -XX-）。"""
    raw = str(proxy or "")
    m = re.search(r"(?i)(?:region|country|cc|geo)[-_=:/]+([a-z]{2})(?=$|[^a-z0-9])", raw)
    if m:
        return m.group(1).upper()
    m = re.search(r"(?i)gate-([a-z]{2})\.[a-z0-9.-]+", raw)
    if m:
        return m.group(1).upper()
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Z]{2})(?=[-_@:/]|$)", raw):
        cc = m.group(1).upper()
        if cc in ("US", "TR", "PH", "GB", "DE", "JP", "VN", "TH", "ID", "IN", "KR", "BR", "MX", "SG", "MY"):
            return cc
    return ""


def _safe_proxy_url(proxy: str) -> str:
    """规范化代理并做 urlsplit 合法性校验；非法返回 ''（防止脏代理导致引擎崩溃）。"""
    normalized = normalize_proxy_url(str(proxy or "").strip())
    if not normalized:
        return ""
    try:
        urlsplit(normalized)
    except ValueError:
        return ""
    return normalized


def _build_candidate_pairs(
    checkout_proxy: str,
    update_proxy: str,
    proxy_pool: list[str],
    exit_pool: list[str],
    checkout_cc: str,
    update_cc: str,
    reuse_checkout_pool_for_update: bool = False,
) -> tuple[list[tuple[str, str]], list[str]]:
    """构造 (建单代理, 促销更新代理) 候选对，对齐源项目 manager 的代理池轮换。

    第一对 = 引擎分配的当前代理；之后从用户代理池按国家标记轮换生成兜底对。
    任何无法解析/轮换失败的代理会被跳过并记录 warning，绝不抛出导致 stdout 无输出。
    """
    checkout_candidates: list[str] = []
    update_candidates: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()

    def _rotate(value: str, cc: str) -> str:
        normalized = _safe_proxy_url(value)
        if not normalized:
            warnings.append(f"代理无法解析，已跳过：{str(value)[:60]}")
            return ""
        if normalized in seen:
            return ""
        seen.add(normalized)
        try:
            return rotate_proxy_session(normalized, cc) if cc else normalized
        except Exception:
            # 轮换失败（如 URL 结构异常）→ 退回原规范化代理，仍可用
            return normalized

    for value in [checkout_proxy, *proxy_pool]:
        rotated = _rotate(value, checkout_cc)
        if rotated:
            checkout_candidates.append(rotated)
    if not checkout_candidates and checkout_proxy:
        safe_fallback = _safe_proxy_url(checkout_proxy)
        if safe_fallback:
            checkout_candidates = [safe_fallback]
    seen = set()
    # 池2留空（且未单独指定出口代理）时，更新阶段严格复用池1的同一条代理。
    # 这样每次重试都是完整的“代理对”切换，而不是建单已换、促销仍粘在首条代理。
    if reuse_checkout_pool_for_update:
        update_candidates = list(checkout_candidates)
    else:
        for value in [update_proxy, *exit_pool]:
            rotated = _rotate(value, update_cc)
            if rotated:
                update_candidates.append(rotated)
        if not update_candidates:
            update_candidates = checkout_candidates[:1]
    pairs: list[tuple[str, str]] = []
    max_len = max(len(checkout_candidates), len(update_candidates))
    for i in range(max_len):
        c = checkout_candidates[i] if i < len(checkout_candidates) else checkout_candidates[-1]
        u = update_candidates[i] if i < len(update_candidates) else update_candidates[-1]
        pair = (c, u)
        if pair not in pairs:
            pairs.append(pair)
    return pairs, warnings


def _run(
    at: str,
    proxy: str,
    proxy_pool: list[str],
    exit_proxy: str,
    exit_pool: list[str],
    country: str,
    currency: str,
    checkout_cc: str,
    update_cc: str,
    promo_campaign_id: str,
    checkout_ui_mode: str,
    skip_proxy_check: bool,
    timeout: int,
    fingerprint: dict | None = None,
    sticky_proxy: bool = False,
    force_zero_checkout: bool = False,
) -> dict:
    t0 = time.monotonic()
    bill_cc = str(country or "PH").strip().upper()
    cur = str(currency or "PHP").strip().upper()
    # 代理期望出口：默认空 = 用户写什么代理就用什么（对齐源项目 manager：不重写国家标记、
    # 不强制出口）；显式填写（如 TR/US）才启用国家轮换与出口校验
    checkout_cc = str(checkout_cc or "").strip().upper()
    update_cc = str(update_cc or "").strip().upper()
    try:
        return _run_impl(
            at, proxy, proxy_pool, exit_proxy, exit_pool, bill_cc, cur,
            checkout_cc, update_cc, promo_campaign_id, checkout_ui_mode,
            skip_proxy_check, timeout, t0, fingerprint, sticky_proxy, force_zero_checkout,
        )
    except Exception as exc:  # 兜底：任何未预期异常都输出 JSON，绝不裸 traceback
        _emit("engine", "fail", f"直卡引擎未预期异常：{type(exc).__name__}: {str(exc)[:200]}")
        return {
            "ok": False, "link": "",
            "error": f"直卡引擎内部错误：{type(exc).__name__}: {str(exc)[:200]}",
            "reason_code": "card_engine_error",
            "extra": {"order_type": "direct_card", "error_stage": "engine",
                      "retryable": False, "error_raw": str(exc),
                      "elapsed_seconds": round(time.monotonic() - t0, 1)},
        }


def _run_impl(
    at: str,
    proxy: str,
    proxy_pool: list[str],
    exit_proxy: str,
    exit_pool: list[str],
    bill_cc: str,
    cur: str,
    checkout_cc: str,
    update_cc: str,
    promo_campaign_id: str,
    checkout_ui_mode: str,
    skip_proxy_check: bool,
    timeout: int,
    t0: float,
    fingerprint: dict | None = None,
    sticky_proxy: bool = False,
    force_zero_checkout: bool = False,
) -> dict:
    checkout_proxy = normalize_proxy_url(proxy)
    update_proxy = normalize_proxy_url(exit_proxy) or checkout_proxy
    pool = [normalize_proxy_url(str(x)) for x in (proxy_pool or []) if str(x or "").strip()]
    xpool = [normalize_proxy_url(str(x)) for x in (exit_pool or []) if str(x or "").strip()]
    reuse_checkout_pool_for_update = not str(exit_proxy or "").strip() and not xpool
    pairs, proxy_warnings = _build_candidate_pairs(
        checkout_proxy, update_proxy, pool, xpool, checkout_cc, update_cc,
        reuse_checkout_pool_for_update=reuse_checkout_pool_for_update,
    )
    # 粘性代理：整个提链过程固定第一对代理（建单+促销更新），IP 不变
    if sticky_proxy:
        pairs = pairs[:1] if pairs else pairs

    _emit("card", "run", f"开始直卡提链（账单={bill_cc}/{cur} 代理=用户原样使用" + (
        f"，建单期望出口={checkout_cc} 促销期望出口={update_cc}" if (checkout_cc or update_cc) else "，不强制国家"
    ) + "）")
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    if sticky_proxy:
        _emit("config", "run", "粘性代理已启用：建单/出口 IP 全程固定（不轮换）")
    if fp:
        _emit("fingerprint", "run",
              "账号指纹 " + str(fp.get("kernel_label") or fp.get("kernel") or "chrome") + "/" + str(fp.get("device_id") or "")[:8]
              + " 内核=" + str(fp.get("kernel") or "?") + " TLS=" + str(fp.get("impersonate") or "?")
              + " 语言=" + str(fp.get("oai_language") or "?") + " 时区=" + str(fp.get("timezone") or "?")
              + " 建单国=" + str(fp.get("country") or bill_cc))
    _emit("config", "run", f"候选代理对={len(pairs)} 条（建单池 {len(pool)} + 出口池 {len(xpool)}）")
    for warning in proxy_warnings:
        _emit("config", "warn", warning)
    if not at:
        return {"ok": False, "link": "", "error": "access_token 为空", "reason_code": "wallet_access_token_missing",
                "extra": {"order_type": "direct_card", "error_stage": "validation", "retryable": False,
                          "elapsed_seconds": round(time.monotonic() - t0, 1)}}
    if not checkout_proxy and not pool:
        _emit("config", "warn", "未提供任何代理（直连尝试，通常会被 ChatGPT 拒绝）")

    last_failure: dict | None = None
    nonzero_pairs: list[dict] = []
    for pair_index, (pair_checkout, pair_update) in enumerate(pairs, start=1):
        _emit("card", "run", f"第 {pair_index}/{len(pairs)} 对代理：建单={pair_checkout[:50]} 更新={pair_update[:50]}")
        # 出口校验仅当用户显式指定期望国家（--checkout/update-proxy-country）且为单代理时启用；
        # 默认（未指定国家）对齐源项目 manager：跳过 preflight，用户写什么代理就用什么
        verify = (not skip_proxy_check) and len(pairs) <= 1 and bool(checkout_cc or update_cc)
        config = ExtractorConfig(
            billing_country=bill_cc,
            currency=cur,
            payment_locale="en",
            # 只有用户显式配置出口国家时才校验或轮换；默认保留代理池原样。
            checkout_proxy_country=checkout_cc,
            update_proxy_country=update_cc,
            checkout_proxy=pair_checkout,
            update_proxy=pair_update,
            promo_campaign_id=str(promo_campaign_id or "").strip(),
            checkout_ui_mode=str(checkout_ui_mode or "hosted").strip(),
            timeout=max(10.0, float(timeout or 45)),
            checkout_attempts=10,
            update_attempts=15,
            full_attempts=3,
            cf_same_identity_attempts=5,
            cf_retry_delay=3.0,
            verify_proxy_country=verify,
            force_zero_checkout=force_zero_checkout,
            fingerprint=fp or None,
        )
        credentials = Credentials(access_token=str(at or "").strip())
        def emit_extractor_log(message: str) -> None:
            # CheckoutExtractor logs are plain text; normalize them into the same
            # [stage] [status] protocol used by PayPal and the web account list.
            msg = str(message or "").strip()
            low = msg.lower()
            if low.startswith("proxy check passed"):
                stage, status = "preflight", "ok"
            elif low.startswith("checkout attempt"):
                stage, status = "checkout", "run"
            elif low.startswith("checkout created"):
                stage, status = "checkout", "ok"
            elif low.startswith("promotion update attempt"):
                stage, status = "promotion", "run"
            elif low.startswith("verifying checkout amount"):
                stage, status = "verify", "run"
            elif low.startswith("checkout amount verified"):
                stage, status = "verify", "ok"
            elif low.startswith("full checkout flow"):
                stage, status = "flow", "run"
            elif "promotion was invalid" in low:
                stage, status = "promotion", "warn"
            elif "cf retries exhausted" in low:
                stage, status = "proxy", "warn"
            elif low.startswith("amount page probe"):
                stage, status = "verify", "warn"
            else:
                stage, status = "card", "run"
            _emit(stage, status, msg)

        try:
            if config.verify_proxy_country:
                _emit("preflight", "run", "正在校验代理出口国家…")
            else:
                _emit("preflight", "ok", "未设置显式代理国家，跳过出口校验")
            extractor = CheckoutExtractor(credentials, config, logger=emit_extractor_log)
            result = extractor.extract()
            elapsed = round(time.monotonic() - t0, 1)
            _emit("resolve", "ok", "直卡 Checkout 长链已提取：" + str(result.long_url)[:100] + "…")
            return {
                "ok": True,
                "link": result.long_url,
                "error": "",
                "reason_code": "",
                "extra": {
                    "order_type": "direct_card",
                    "link_type": "direct_card_checkout_link",
                    "amount_minor": result.amount_minor,
                    "amount": result.amount_minor if result.amount_minor is not None else 0,
                    "currency": result.amount_currency or cur,
                    "billing_country": result.billing_country or bill_cc,
                    "processor_entity": result.processor_entity or "",
                    "cs_id": result.cs_id or "",
                    "amount_verification": result.amount_verification or "",
                    "checkout_ui_mode": str(checkout_ui_mode or "hosted"),
                    "error_stage": "",
                    "retryable": False,
                    "elapsed_seconds": elapsed,
                    "proxy_pair_index": pair_index,
                },
            }
        except NonZeroAmountError as exc:
            elapsed = round(time.monotonic() - t0, 1)
            msg = _translate_error("NonZeroAmountError", str(exc), "promotion")
            # 金额已经完成校验：NonZero 是账号/促销资格的确定性结果，
            # 不是网络或代理故障，不能继续遍历代理池浪费时间。
            _emit("promotion", "fail", f"第 {pair_index} 对代理确认金额非 0，停止重试")
            return {"ok": False, "link": "", "error": msg, "reason_code": "NonZeroAmountError",
                    "extra": {"order_type": "direct_card", "error_stage": "promotion",
                              "retryable": False, "elapsed_seconds": elapsed,
                              "proxy_pair_index": pair_index,
                              "proxy_attempts": [{"pair_index": pair_index, "error": msg}]}}
        except (ExtractorError, UpstreamError, InvalidPromotionError) as exc:
            elapsed = round(time.monotonic() - t0, 1)
            error_type = type(exc).__name__
            raw_error = str(exc)
            low_error = raw_error.lower()
            # 与 PayPal 一致：只有网络/限流/Cloudflare/5xx 等可恢复错误换下一对代理；
            # 凭证、账号资格、促销业务错误立即结束，避免无意义重试。
            retryable = (
                isinstance(exc, UpstreamError) and bool(getattr(exc, "retryable", False))
            ) or any(marker in low_error for marker in (
                "proxy country mismatch", "timed out", "timeout", "connection", "ssl",
                "curl: (", "network error", "connection aborted", "proxy connect aborted",
                "unexpected_eof", "eof occurred in violation of protocol", "max retries exceeded",
                "http 408", "http 429", "http 500", "http 502", "http 503", "http 504"
            ))
            stage = "proxy" if "proxy country mismatch" in low_error else (
                "promotion" if isinstance(exc, InvalidPromotionError) or "promotion" in low_error else "checkout"
            )
            msg = _translate_error(error_type, raw_error, stage)
            _emit(stage, "fail", f"第 {pair_index} 对代理失败：{msg[:160]}")
            last_failure = {"ok": False, "link": "", "error": msg, "reason_code": error_type,
                            "extra": {"order_type": "direct_card", "error_stage": stage,
                                      "retryable": retryable, "elapsed_seconds": elapsed,
                                      "error_raw": raw_error,
                                      "proxy_pair_index": pair_index}}
            if retryable and pair_index < len(pairs):
                _emit("card", "warn", f"可重试错误，切换下一对代理（{pair_index + 1}/{len(pairs)}）")
                continue
            break
        except Exception as exc:
            elapsed = round(time.monotonic() - t0, 1)
            msg = _translate_error(type(exc).__name__, str(exc), "engine")
            last_failure = {"ok": False, "link": "", "error": msg, "reason_code": "unexpected_error",
                            "extra": {"order_type": "direct_card", "error_stage": "engine",
                                      "retryable": False, "elapsed_seconds": elapsed,
                                      "error_raw": str(exc)}}
            break
    return last_failure or {"ok": False, "link": "", "error": "直卡提链失败（未知）", "reason_code": "direct_card_failed",
                            "extra": {"order_type": "direct_card", "error_stage": "engine", "retryable": False}}


def _parse_pool(raw: str) -> list[str]:
    if not str(raw or "").strip():
        return []
    text = str(raw or "").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except json.JSONDecodeError:
        pass
    # 兼容多行/逗号文本；某些外壳会剥掉 JSON 引号（如 [a,b]），先去括号再切分
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [line.strip() for line in re.split(r"[\r\n,;]+", text) if line.strip()]


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


def main() -> int:
    parser = argparse.ArgumentParser(description="直卡提链服务 CLI（对齐源项目）")
    parser.add_argument("--at", required=True, help="access_token")
    parser.add_argument("--proxy", default="", help="建单代理 (host:port[:user:pass] 或 URL)")
    parser.add_argument("--exit-proxy", default="", help="促销更新代理（可空=同建单代理）")
    parser.add_argument("--proxy-pool", default="", help="用户建单代理池 JSON 数组")
    parser.add_argument("--exit-pool", default="", help="用户促销更新代理池 JSON 数组（可空=复用主池）")
    parser.add_argument("--country", default="PH", help="账单国家（默认 PH）")
    parser.add_argument("--currency", default="PHP", help="货币（默认 PHP）")
    parser.add_argument("--checkout-proxy-country", default="", help="建单代理期望出口（默认空=不强制，用户写什么就用什么；填写后启用出口校验与轮换，如 TR/US）")
    parser.add_argument("--update-proxy-country", default="", help="促销更新代理期望出口（默认空=不强制，用户写什么就用什么）")
    parser.add_argument("--checkout-ui-mode", default="custom", help="checkout UI 模式（hosted/custom）")
    parser.add_argument("--promo-campaign-id", default="plus-1-month-free", help="促销活动 ID")
    parser.add_argument("--skip-proxy-check", action="store_true", help="跳过代理出口校验（代理池模式自动开启）")
    parser.add_argument("--timeout", default="45", help="单请求超时秒数")
    parser.add_argument("--fingerprint", default="", help="账号指纹档案 JSON（device_id/ua/impersonate/语言/时区，按建单国家分配）")
    parser.add_argument("--sticky-proxy", default="0", help="粘性代理 (0/1)：整个提链过程固定建单/出口代理，IP 不变")
    parser.add_argument("--force-zero", action="store_true", help="强压 0 元：金额非 0 时仍继续确认并输出链接")
    parser.add_argument("--json", default="", help="(兼容参数，忽略)")
    args = parser.parse_args()

    result = _run(
        at=args.at,
        proxy=args.proxy,
        proxy_pool=_parse_pool(args.proxy_pool),
        exit_proxy=args.exit_proxy,
        exit_pool=_parse_pool(args.exit_pool),
        country=args.country,
        currency=args.currency,
        checkout_cc=args.checkout_proxy_country,
        update_cc=args.update_proxy_country,
        promo_campaign_id=args.promo_campaign_id,
        checkout_ui_mode=args.checkout_ui_mode,
        skip_proxy_check=args.skip_proxy_check,
        timeout=int(args.timeout or 45),
        fingerprint=_parse_fingerprint(args.fingerprint),
        sticky_proxy=str(args.sticky_proxy or "0").strip() in {"1", "true", "yes", "on"},
        force_zero_checkout=bool(args.force_zero),
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
