from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from upi_link_service import extract_upi_link, localize_upi_error

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# 对外日志统一使用 MoMo 的阶段协议，具体 UPI 动作保留在 detail 中。
# 这样并发日志可以直接按相同 stage 对比两条支付链路。
UPI_LOG_STAGE_MAP = {
    "代理准备": "proxy_prepare",
    "登录会话刷新": "auth",
    "优惠资格预检": "promo",
    "重新建单": "checkout",
    "重新建单重试": "checkout",
    "页面预热": "warmup",
    "风控上下文": "sentinel",
    "浏览器指纹": "sentinel",
    "创建支付订单": "checkout",
    "应用0元优惠": "state",
    "选择UPI支付通道": "state",
    "进入印度支付页": "state",
    "支付页预热": "state",
    "初始化Stripe支付页": "init",
    "初始化UPI支付组件": "elements",
    "更新Stripe税费国家": "taxes",
    "提交印度账单与税费": "taxes",
    "0元金额校验": "amount",
    "保存账单快照": "billing",
    "更新Stripe账单地址": "billing",
    "生成支付确认令牌": "token",
    "订单确认风控校验": "confirm",
    "确认UPI自定义通道": "confirm",
    "提交Stripe支付确认": "confirm",
    "确认ChatGPT订单": "confirm",
    "确认UPI支付意图": "confirm",
    "ChatGPT订单风控审批": "approve",
    "查询0元支付状态": "poll",
    "启动UPI自定义通道": "gateway",
    "生成支付链接和二维码": "gateway",
}


def emit_step(steps: list[dict[str, str]]) -> None:
    item = steps[-1] if steps else {}
    raw_name = str(item.get("name") or "UPI流程")
    name = UPI_LOG_STAGE_MAP.get(raw_name, raw_name)
    status = str(item.get("status") or "info")
    # 阶段详情可能包含 Playwright 的多行 Call log；压成单行，保证 Node
    # 能持续按行解析后续最终 JSON，不会被浏览器诊断拆散。
    detail = " ".join(str(item.get("detail") or "").split())
    # 与 MoMo 子进程保持同一日志协议：[stage] [ok/run/warn/fail] detail。
    # 通道名由 Node 调度层和任务本身确定，不在 stage 中重复添加。
    print(f"[{name}] [{status}] {detail[:800]}", flush=True)


def emit_qr_event(payload: dict) -> None:
    """Stream QR/payment-status events for the Node process."""
    try:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="UPI HAR-aligned extraction CLI")
    parser.add_argument("--at", required=True)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--proxy-pool", default="", help="JSON proxy pool used only for SetupIntent stage rotation")
    parser.add_argument("--promotion-proxy", default="")
    parser.add_argument("--email", default="")
    parser.add_argument("--billing-json", default="")
    parser.add_argument("--fingerprint", default="", help="账号指纹档案 JSON")
    parser.add_argument("--promo-campaign-id", default="plus-1-month-free")
    parser.add_argument("--checkout-ui-mode", default="auto", help="auto=按 oaics_/cs_live_ 会话前缀自动分流；custom/hosted/deferred 显式指定")
    parser.add_argument("--upi-rail", default="upi", help="旧版 UPI rail 名称：upi/upi-git/upi-git2/upi-git3/upi-git4/upi-git5")
    parser.add_argument("--upi-setup-v2", action="store_true", help="兼容旧版 upi-git2～upi-git5 Setup v2 标记")
    parser.add_argument("--require-server-upi-mandate", action="store_true", help="兼容旧版 upi-git4 mandate_options 校验")
    parser.add_argument("--requested-upi-mandate", default="", help="旧版 UPI mandate_options JSON")
    parser.add_argument("--upi-har-context", action="store_true", help="兼容旧版 upi-git5 HAR context 标记")
    parser.add_argument("--force-payment-method", action="store_true", help="payment_method_types 无 upi 时仍强制提交 upi 支付方式")
    parser.add_argument("--force-zero", action="store_true", help="未确认 0 元时仍继续 confirm/start（强压 0 元）")
    parser.add_argument("--wait-paid", action="store_true", help="链接返回后保持同一会话，持续刷新二维码并等待到账")
    parser.add_argument("--paid-timeout", type=float, default=900.0, help="等待到账秒数（默认 900）")
    args = parser.parse_args()

    billing: dict[str, Any] = {"email": str(args.email or "").strip()}
    if str(args.billing_json or "").strip():
        try:
            parsed = json.loads(args.billing_json)
            if isinstance(parsed, dict):
                billing.update(parsed)
        except json.JSONDecodeError:
            result = {"ok": False, "link": "", "error": "UPI 账单参数格式错误，请检查账单 JSON 内容"}
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
            return 1

    fingerprint: dict[str, Any] = {}
    if str(args.fingerprint or "").strip():
        try:
            parsed_fp = json.loads(args.fingerprint)
            if isinstance(parsed_fp, dict):
                fingerprint = parsed_fp
        except json.JSONDecodeError:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "link": "",
                        "error": "UPI 指纹参数格式错误",
                        "reason_code": "upi_fingerprint_invalid",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return 1

    proxy_pool: list[str] = []
    if str(args.proxy_pool or "").strip():
        try:
            parsed_pool = json.loads(str(args.proxy_pool))
            if isinstance(parsed_pool, list):
                proxy_pool = [str(item).strip() for item in parsed_pool if str(item).strip()]
        except json.JSONDecodeError:
            proxy_pool = []

    requested_upi_mandate: dict[str, Any] = {}
    if str(args.requested_upi_mandate or "").strip():
        try:
            parsed_mandate = json.loads(str(args.requested_upi_mandate))
            if isinstance(parsed_mandate, dict):
                requested_upi_mandate = parsed_mandate
        except json.JSONDecodeError:
            result = {"ok": False, "link": "", "error": "UPI mandate 参数格式错误，请检查 JSON 内容", "reason_code": "upi_mandate_invalid"}
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
            return 1

    try:
        result = extract_upi_link(
            str(args.at or "").strip(),
            proxy=str(args.proxy or "").strip(),
            proxy_pool=proxy_pool,
            promotion_proxy=str(args.promotion_proxy or "").strip(),
            billing=billing,
            fingerprint=fingerprint,
            on_step=emit_step,
            on_qr_update=emit_qr_event,
            use_promo=True,
            promo_campaign=str(args.promo_campaign_id or "").strip() or None,
            checkout_ui_mode=str(args.checkout_ui_mode or "auto").strip().lower(),
            force_payment_method=bool(args.force_payment_method),
            force_zero=bool(args.force_zero),
            wait_paid=bool(args.wait_paid),
            paid_timeout=max(30.0, float(args.paid_timeout)),
            upi_rail=str(args.upi_rail or "upi").strip() or "upi",
            upi_setup_v2=bool(args.upi_setup_v2),
            require_server_upi_mandate=bool(args.require_server_upi_mandate),
            requested_upi_mandate=requested_upi_mandate,
            upi_har_context=bool(args.upi_har_context),
        )
        link = str(
            result.get("provider_redirect_url")
            or result.get("long_url")
            or result.get("url")
            or ""
        ).strip()
        output = dict(result)
        output.update(
            {
                "ok": bool(result.get("ok")) and bool(link),
                "link": link,
                "provider": "upi",
                "reason_code": "" if link else "upi_link_missing",
            }
        )
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 0 if output["ok"] else 1
    except Exception as exc:
        raw_message = str(exc)[:1200]
        # 排障诊断：把原始异常与调用栈一并透传给 Node（error_raw），供服务端
        # console/任务快照查看；本地化中文仅用于面向用户的展示与重试归类。
        try:
            import traceback as _tb
            _tb_text = _tb.format_exc(limit=10)
            raw_detail = (raw_message + "\n" + _tb_text)[-2200:]
        except Exception:
            raw_detail = raw_message
        message = localize_upi_error(raw_message)[:1200]
        lower = (raw_message + " " + message).lower()
        if (
            "账号无优惠资格" in message
            or "this promotion is not available" in lower
            or "promo not eligible" in lower
            or "promotion update failed: http 403" in lower
            or "upi promo not eligible" in lower
            or "upi promo already redeemed" in lower
        ):
            reason = "upi_promo_ineligible"
            # 优惠资格与建单代理出口强相关：同一账号在部分代理上有资格、在另一些
            # 上没有。资格失败必须可换代理重试，否则会在建单之前把账号判死。
            # 实为账号级终态时，Worker 仍会依据任务级重试预算收敛。
            retryable = True
        elif (
            "优惠未应用" in message
            or "upi promo not applied" in lower
            or "upi promo amount unavailable" in lower
        ):
            reason = "upi_promo_not_applied"
            retryable = False
        elif (
            "checkout_approval_payment_failure_with_payment_error" in lower
            or "setup_attempt_failed" in lower
            or "generic_decline" in lower
            or "upi_payment_setup_declined" in lower
            or "支付设置被 stripe 拒绝" in message.lower()
            or "upi custom checkout submission failed" in lower
        ):
            reason = "upi_payment_setup_declined"
            # Stripe has already rejected the payment setup for this
            # Checkout session. Treat explicit setup_attempt_failed/
            # generic_decline as terminal in the distributed worker; a new
            # proxy/fingerprint must not be used to repeatedly submit a
            # blocked payment attempt. The original diagnostic is retained.
            retryable = False
        elif (
            "deferred-intent prerequisites missing" in lower
            or "payment prerequisites missing" in lower
            or "支付前置字段缺失" in message
        ):
            reason = "upi_checkout_prerequisites_missing"
            retryable = True
        elif (
            "checkout confirm blocked" in lower
            or "submission blocked" in lower
            or "risk_blocked" in lower
            or "result=blocked" in lower
            or "http 403" in lower
            or "status=blocked" in lower
            or "status': 'blocked" in lower
            or "风控拦截" in message
        ):
            reason = "upi_risk_blocked"
            retryable = False
        elif "部署证明缺失" in raw_message or "风控上下文生成失败" in message:
            reason = "upi_risk_context_missing"
            retryable = False
        elif "proxy is required" in lower or "缺少代理" in message:
            reason = "upi_proxy_config_missing"
            retryable = False
        elif "access token is required" in lower or "缺少账号登录凭证" in message:
            reason = "upi_auth_invalid"
            retryable = False
        elif (
            "invalidated" in lower
            or "登录凭证已失效" in message
            or "凭证已失效" in message
            or "unauthorized" in lower
            or "http 401" in lower
        ):
            reason = "upi_auth_invalid"
            retryable = False
        elif "upi cpmt" in lower or "自定义支付方式通道不可用" in message:
            reason = "upi_cpmt_unavailable"
            retryable = False
        elif "checkout approve" in lower or "订单审批" in message:
            reason = "upi_approve_rejected"
            retryable = True
        elif (
            "network request failed" in lower
            or "网络请求失败" in message
            or "代理连接失败" in message
            or "代理网络超时" in message
            or "代理安全连接异常" in message
            or "proxy" in lower
            or "curl:" in lower
            or "timed out" in lower
        ):
            reason = "upi_proxy_network_error"
            retryable = True
        else:
            reason = "upi_extract_failed"
            retryable = True
        output = {
            "ok": False,
            "link": "",
            "error": message,
            "error_raw": raw_detail,
            "provider": "upi",
            "reason_code": reason,
            "retryable": retryable,
        }
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
