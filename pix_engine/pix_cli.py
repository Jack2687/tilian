from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from pix_service import demo_result, extract_pix_link, localize_pix_error

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def emit_step(step: dict[str, str]) -> None:
    print(f"[pix:{step.get('name', 'step')}] [{step.get('status', 'info')}] {step.get('detail', '')[:800]}", flush=True)


def emit_qr(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def emit_paid(status: str) -> None:
    """支付完成事件（GCash 对齐）：同一会话确认到账后输出机器可读事件。"""
    try:
        print(f"[paid] [ok] payment landed; status={status}", flush=True)
    except Exception:
        pass
    try:
        print(json.dumps({
            "event": "payment_status",
            "payment_status": str(status or ""),
            "paid": str(status or "") == "plus_active",
            "qr_refresh_active": False,
            "updated_at": __import__("time").time(),
        }, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="PIX Stripe Custom Checkout extraction CLI")
    parser.add_argument("--at", default="")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--proxy-pool", default="",
                        help="用户代理池 JSON 数组字符串；主代理网络失败时按序换池重跑整段")
    parser.add_argument("--country", default="BR")
    parser.add_argument("--currency", default="BRL")
    parser.add_argument("--billing-json", default="")
    parser.add_argument("--tax-kind", default="", choices=["", "cpf", "cnpj", "random"],
                        help="auto tax id kind when billing lacks tax_id; random/cnpj use the real registered CNPJ pool (default: random -> real CNPJ), cpf fabricates a checksum-valid CPF that the PIX PSP usually rejects")
    parser.add_argument("--promo-campaign-id", default="plus-1-month-free")
    parser.add_argument("--no-require-zero", action="store_true")
    parser.add_argument("--force-payment-method", action="store_true", help="强制继续提交 PIX 支付方式，即使 Checkout 能力列表未回显 pix")
    parser.add_argument("--force-zero", action="store_true", help="强压 0 元流程：跳过前置/最终金额拦截并继续确认")
    parser.add_argument("--max-poll-attempts", type=int, default=120)
    parser.add_argument("--confirm-protocol", default="", choices=["", "auto", "token", "legacy"],
                        help="OA 确认协议：auto=优先 confirm-token 新链路、契约错误回退 legacy（默认）；token=强制新链路；legacy=强制 payment_pages 旧链路")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--wait-paid", action="store_true",
                        help="keep the same session alive after the QR is handed off and wait until the payment actually lands (Stripe submission succeeded/complete or Plus subscription active), emitting [paid]/payment_status events (GCash-aligned)")
    parser.add_argument("--paid-timeout", type=float, default=900.0,
                        help="seconds to keep watching for payment confirmation when --wait-paid (default 900)")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    if args.demo:
        output = demo_result()
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 0
    billing: dict[str, Any] = {}
    if args.billing_json:
        try:
            value = json.loads(args.billing_json)
            if not isinstance(value, dict):
                raise ValueError("billing JSON must be an object")
            billing = value
        except (json.JSONDecodeError, ValueError) as exc:
            output = {"ok": False, "link": "", "provider": "pix", "error": f"PIX 账单参数格式错误: {exc}", "reason_code": "pix_billing_invalid", "retryable": False}
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":")), flush=True)
            return 1
    if args.tax_kind and not billing.get("tax_kind"):
        billing["tax_kind"] = args.tax_kind
    proxy_pool: list[str] = []
    if str(args.proxy_pool or "").strip():
        try:
            value = json.loads(str(args.proxy_pool).strip())
            if isinstance(value, list):
                proxy_pool = [str(item) for item in value]
        except json.JSONDecodeError:
            proxy_pool = []
    try:
        fingerprint = {}
        if args.fingerprint:
            value = json.loads(args.fingerprint)
            if isinstance(value, dict):
                fingerprint = value
        result = extract_pix_link(
            args.at, args.proxy, billing=billing, promo_campaign_id=args.promo_campaign_id,
            require_zero=not args.no_require_zero, max_poll_attempts=max(1, args.max_poll_attempts),
            force_payment_method=args.force_payment_method, force_zero_checkout=args.force_zero,
            country=args.country, currency=args.currency, proxy_pool=proxy_pool,
            oa_confirm_protocol=args.confirm_protocol,
            on_step=emit_step, on_qr_update=emit_qr, fingerprint=fingerprint,
            on_paid=emit_paid if args.wait_paid else None,
            wait_paid=args.wait_paid, paid_timeout=max(30.0, args.paid_timeout),
        )
    except Exception as exc:
        result = {"ok": False, "link": "", "provider": "pix", "error": localize_pix_error(exc), "reason_code": "pix_extract_failed", "retryable": True}
    link = str(result.get("provider_redirect_url") or result.get("link") or result.get("url") or "").strip()
    result["link"] = link
    result["ok"] = bool(result.get("ok")) and bool(link)
    result.setdefault("provider", "pix")
    result.setdefault("reason_code", "" if result["ok"] else "pix_link_missing")
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
