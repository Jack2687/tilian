from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .proxy import normalize_proxy_pool, normalize_proxy_url
from .python_runtime import resolve_python
from .engine_protocol import public_engine_extra
from .wallet_results import wallet_extra


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMO = "plus-1-month-free"
PAYMENT_MODES = {"gopay", "gcash", "momo", "upi", "blik", "pix", "ideal", "card"}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _params(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("params")
    return value if isinstance(value, dict) else {}


def _is_true(value: Any) -> bool:
    return value is True


def _bool_param(params: dict[str, Any], *names: str) -> bool:
    return any(_is_true(params.get(name)) for name in names)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = _text(value)
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _seconds(value: Any, minimum: float) -> str | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return _format_number(max(minimum, number))


def _integer_ceil(value: Any, minimum: int) -> str | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return str(max(minimum, math.ceil(number)))


def _timeout_seconds(value: Any, minimum: int) -> str | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return str(max(minimum, math.ceil(number / 1000)))


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _fingerprint(payload: dict[str, Any], params: dict[str, Any]) -> dict[str, Any] | None:
    value = params.get("fingerprint")
    if not isinstance(value, dict):
        value = payload.get("fingerprint")
    return value if isinstance(value, dict) and value else None


def _append_fingerprint(command: list[str], payload: dict[str, Any], params: dict[str, Any]) -> None:
    fingerprint = _fingerprint(payload, params)
    if fingerprint:
        command += ["--fingerprint", json.dumps(fingerprint, ensure_ascii=False, separators=(",", ":"))]


def _append_require_zero(command: list[str], params: dict[str, Any]) -> None:
    command.append("--no-require-zero" if params.get("requireZero") is False else "--require-zero")


def _force_payment(params: dict[str, Any]) -> bool:
    return _bool_param(params, "forcePaymentMethod", "forceMethod")


def _force_zero(params: dict[str, Any]) -> bool:
    return _bool_param(params, "forceZeroCheckout", "forceZero")


def _is_zero(value: Any) -> bool:
    number = _number(value)
    return number is not None and number == 0


def _stripe_qr_image(url: str) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    return parsed.netloc.lower() == "qr.stripe.com" and bool(re.search(r"\.(?:png|svg)$", parsed.path, flags=re.IGNORECASE))


def _stripe_upi_hosted_instructions(url: str) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    return parsed.netloc.lower() == "payments.stripe.com" and parsed.path.startswith("/upi/instructions/")


class EngineAdapter:
    def __init__(self, timeout_seconds: int = 600) -> None:
        self.timeout_seconds = timeout_seconds

    def command(
        self,
        mode: str,
        at: str,
        email: str,
        proxy: str,
        payload: dict[str, Any],
        exit_proxy: str = "",
    ) -> list[str]:
        mode = str(mode or "").strip().lower()
        params = _params(payload)
        country = _text(params.get("country"))
        currency = _text(params.get("currency"))
        promo = _text(params.get("promoCampaignId")) or DEFAULT_PROMO
        checkout_ui_mode = _text(params.get("checkoutUiMode"))
        force_payment = _force_payment(params)
        force_zero = _force_zero(params)
        proxy_url = normalize_proxy_url(proxy) if proxy else ""
        exit_proxy_url = normalize_proxy_url(exit_proxy) if exit_proxy else ""
        pool = normalize_proxy_pool(payload.get("proxiesText") or "", "建单代理池")
        exit_pool = normalize_proxy_pool(payload.get("exitProxiesText") or "", "出口代理池")
        pool_json = json.dumps(pool, ensure_ascii=False, separators=(",", ":"))
        exit_pool_json = json.dumps(exit_pool, ensure_ascii=False, separators=(",", ":"))
        python = resolve_python()

        if mode == "paypal":
            command = [python, str(ROOT / "engines/sms_tool/link_service.py"), "--at", at, "--mode", "paypal", "--json", ""]
            if proxy_url:
                command += ["--proxy", proxy_url]
            if exit_proxy_url:
                command += ["--exit-proxy", exit_proxy_url]
            _append_fingerprint(command, payload, params)
            if country:
                command += ["--country", country]
            command.append("--auto")
            return command

        if mode == "card":
            command = [
                python,
                str(ROOT / "card_engine/card_service.py"),
                "--at",
                at,
                "--country",
                country or "PH",
                "--currency",
                currency or "PHP",
                "--json",
                "",
            ]
            if proxy_url:
                command += ["--proxy", proxy_url]
            if exit_proxy_url:
                command += ["--exit-proxy", exit_proxy_url]
            _append_fingerprint(command, payload, params)
            if params.get("stickyProxy") is not False:
                command += ["--sticky-proxy", "1"]
            if pool:
                command += ["--proxy-pool", pool_json]
            if exit_pool:
                command += ["--exit-pool", exit_pool_json]
            if checkout_ui_mode:
                command += ["--checkout-ui-mode", checkout_ui_mode]
            if promo:
                command += ["--promo-campaign-id", promo]
            if force_zero:
                command.append("--force-zero")
            timeout = _timeout_seconds(params.get("timeoutMs"), 10)
            if timeout:
                command += ["--timeout", timeout]
            return command

        if mode == "gopay":
            command = [python, str(ROOT / "gopay_engine/gopay_service.py"), "--at", at, "--proxy", proxy_url, "--json", ""]
            if country:
                command += ["--country", country]
            if currency:
                command += ["--currency", currency]
            _append_fingerprint(command, payload, params)
            if pool:
                command += ["--proxy-pool", pool_json]
            if exit_proxy_url:
                command += ["--exit-proxy", exit_proxy_url]
            if promo:
                command += ["--promo-campaign-id", promo]
            if checkout_ui_mode:
                command += ["--checkout-ui-mode", checkout_ui_mode]
            _append_require_zero(command, params)
            if force_payment:
                command.append("--force-payment-method")
            if force_zero:
                command.append("--force-zero")
            if _bool_param(params, "gopayBrowserFallback"):
                command += ["--gopay-browser-fallback", "1"]
            return command

        if mode == "gcash":
            command = [python, str(ROOT / "gcash_engine/gcash_service.py"), "--at", at, "--proxy", proxy_url, "--json", ""]
            if country:
                command += ["--country", country]
            if currency:
                command += ["--currency", currency]
            _append_fingerprint(command, payload, params)
            if pool:
                command += ["--proxy-pool", pool_json]
            if exit_proxy_url:
                command += ["--exit-proxy", exit_proxy_url]
            if promo:
                command += ["--promo-campaign-id", promo]
            if checkout_ui_mode:
                command += ["--checkout-ui-mode", checkout_ui_mode]
            _append_require_zero(command, params)
            return command

        if mode == "momo":
            session_token = _text(_first_present(params, "sessionToken", "session_token"))
            command = [
                python,
                str(ROOT / "momo_engine/momo_service.py"),
                "--at",
                at,
                "--proxy",
                proxy_url,
                "--exit-proxy",
                exit_proxy_url,
                "--country",
                country or "VN",
                "--currency",
                currency or "VND",
                "--promo-campaign-id",
                promo,
                "--session-token",
                session_token,
            ]
            _append_require_zero(command, params)
            if force_payment:
                command.append("--force-payment-method")
            if force_zero:
                command.append("--force-zero")
            if checkout_ui_mode:
                command += ["--checkout-ui-mode", checkout_ui_mode]
            _append_fingerprint(command, payload, params)
            return command

        if mode == "blik":
            command = [python, str(ROOT / "blik_engine/blik_service.py"), "--at", at, "--proxy", proxy_url]
            if email:
                command += ["--email", email]
            _append_fingerprint(command, payload, params)
            if promo:
                command += ["--promo-campaign-id", promo]
            paid_timeout = _seconds(params.get("paidTimeout"), 30)
            if paid_timeout:
                command += ["--paid-timeout", paid_timeout]
            return command

        if mode == "upi":
            # UPI is a single-chain link type: the reference submits one India
            # proxy pool and reuses it for both the promotion preflight and the
            # Checkout/Stripe chain (app.py: "entry_proxies": [...],
            # "exit_proxies": list(entry_proxies), sticky_proxy=true).  A
            # separate exit pool would move the egress country between the
            # eligibility check and Checkout, which the engine explicitly
            # guards against with PROMOTION_REGION = PROVIDER_REGION = 'IN'.
            upi_proxy = proxy_url or exit_proxy_url
            command = [python, str(ROOT / "engines/pay153-checkout-link-main/upi_cli.py"), "--at", at, "--proxy", upi_proxy]
            promotion_proxy = proxy_url or upi_proxy
            if promotion_proxy:
                command += ["--promotion-proxy", promotion_proxy]
            if email:
                command += ["--email", email]
            billing = params.get("billing")
            if isinstance(billing, dict) and billing:
                # Keep caller-provided, self-consistent IN billing available to
                # the engine.  The engine still validates/normalizes it and
                # generates a random IN address when omitted, matching the
                # reference flow's billing fallback.
                command += ["--billing-json", json.dumps(billing, ensure_ascii=False, separators=(",", ":"))]
            if promo:
                command += ["--promo-campaign-id", promo]
            if checkout_ui_mode:
                command += ["--checkout-ui-mode", checkout_ui_mode]
            # Default to the source worker's upi-git5 rail: 纯协议 · 无浏览器内核。
            # 调用方仍可显式传 upiRail/upi_rail 退回浏览器 rail（upi/upi-git..git4），
            # 这条参数就是协议模式的回滚开关。
            upi_rail = _text(params.get("upiRail") or params.get("linkType") or "upi-git5")
            if upi_rail:
                command += ["--upi-rail", upi_rail]
            if _bool_param(params, "upiSetupV2"):
                command.append("--upi-setup-v2")
            if _bool_param(params, "requireServerUpiMandate"):
                command.append("--require-server-upi-mandate")
            requested_mandate = params.get("requestedUpiMandate")
            if isinstance(requested_mandate, dict) and requested_mandate:
                command += ["--requested-upi-mandate", json.dumps(requested_mandate, ensure_ascii=False, separators=(",", ":"))]
            if _bool_param(params, "upiHarContext"):
                command.append("--upi-har-context")
            if force_payment:
                command.append("--force-payment-method")
            if force_zero:
                command.append("--force-zero")
            if _bool_param(params, "waitPaid"):
                command.append("--wait-paid")
                paid_timeout = _seconds(params.get("paidTimeout"), 30)
                if paid_timeout:
                    command += ["--paid-timeout", paid_timeout]
            _append_fingerprint(command, payload, params)
            return command

        if mode == "pix":
            billing = params.get("billing")
            billing_value = dict(billing) if isinstance(billing, dict) else None
            if email:
                billing_value = {**(billing_value or {}), "email": _text((billing_value or {}).get("email")) or email}
            command = [
                python,
                str(ROOT / "pix_engine/pix_cli.py"),
                "--at",
                at,
                "--proxy",
                proxy_url or exit_proxy_url,
                "--country",
                country or "BR",
                "--currency",
                currency or "BRL",
            ]
            if pool:
                command += ["--proxy-pool", pool_json]
            if billing_value is not None:
                command += ["--billing-json", json.dumps(billing_value, ensure_ascii=False, separators=(",", ":"))]
            _append_fingerprint(command, payload, params)
            if promo:
                command += ["--promo-campaign-id", promo]
            if params.get("requireZero") is False:
                command.append("--no-require-zero")
            if force_payment:
                command.append("--force-payment-method")
            if force_zero:
                command.append("--force-zero")
            max_poll_attempts = _integer_ceil(params.get("maxPollAttempts"), 1)
            if max_poll_attempts:
                command += ["--max-poll-attempts", max_poll_attempts]
            if _bool_param(params, "waitPaid"):
                command.append("--wait-paid")
                paid_timeout = _seconds(params.get("paidTimeout"), 30)
                if paid_timeout:
                    command += ["--paid-timeout", paid_timeout]
            return command

        if mode == "ideal":
            command = [python, str(ROOT / "ideal_engine/ideal_service.py"), "--at", at, "--proxy", proxy_url, "--json", ""]
            if pool:
                command += ["--proxy-pool", pool_json]
            if exit_proxy_url:
                command += ["--exit-proxy", exit_proxy_url]
            command += ["--country", country or "NL", "--currency", currency or "EUR"]
            if promo:
                command += ["--promo-campaign-id", promo]
            if params.get("requireZero") is False:
                command.append("--no-require-zero")
            if force_payment:
                command.append("--force-payment-method")
            if force_zero:
                command.append("--force-zero")
            ideal_bank = _text(_first_present(params, "idealBank", "ideal_bank"))
            if ideal_bank:
                command += ["--ideal-bank", ideal_bank]
            if email:
                command += ["--email", email]
            timeout = _timeout_seconds(params.get("timeoutMs"), 10)
            if timeout:
                command += ["--timeout", timeout]
            command += ["--sticky-proxy", "0" if params.get("stickyProxy") is False else "1"]
            _append_fingerprint(command, payload, params)
            return command

        raise ValueError(f"unsupported engine mode: {mode}")

    def environment(self, mode: str, payload: dict[str, Any]) -> dict[str, str]:
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        if str(mode or "").strip().lower() == "gcash":
            params = _params(payload)
            force_cpmt = _bool_param(params, "forceGcashCpmt", "forcePaymentMethod", "forceMethod")
            force_zero = _force_zero(params)
            environment["GCASH_FORCE_CPMT"] = "1" if force_cpmt else "0"
            environment["GCASH_FORCE_ZERO"] = "1" if force_zero else "0"
        return environment

    def _normalize_result(self, mode: str, raw: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
        mode = str(mode or "").strip().lower()
        source = dict(raw) if isinstance(raw, dict) else {}
        result = dict(source)
        params = params if isinstance(params, dict) else {}
        extra = wallet_extra(mode, source, params) if mode in {"paypal", "gopay", "gcash"} else (source.get("extra") if isinstance(source.get("extra"), dict) else {})
        if mode in {"upi", "pix", "blik"}:
            extra = public_engine_extra(source)
        result["extra"] = extra
        retryable = source.get("retryable")
        if retryable is None and "retryable" in extra:
            retryable = extra.get("retryable")
        if retryable is not None:
            result["retryable"] = bool(retryable)
        engine_ok = source.get("ok") is True

        def fail(default_error: str, default_reason: str, forced_error: str = "") -> dict[str, Any]:
            existing_error = _text(result.get("error"))
            result["ok"] = False
            result["link"] = ""
            result["error"] = forced_error or existing_error or default_error
            existing_reason = _text(result.get("reason_code")) or _text(extra.get("reason_code")) or _text(extra.get("error_stage"))
            result["reason_code"] = existing_reason or default_reason
            return result

        label = {
            "paypal": "PayPal",
            "card": "直卡",
            "gopay": "GoPay",
            "gcash": "GCash",
            "momo": "MoMo",
            "blik": "BLIK",
            "upi": "UPI",
            "pix": "PIX",
            "ideal": "ideal",
        }.get(mode, mode or "引擎")

        if not engine_ok:
            return fail(f"{label} 提链失败", f"{mode or 'engine'}_engine_failed")

        # Engines may report an otherwise successful checkout envelope while
        # explicitly saying that the requested payment method is unavailable.
        # Stop before exposing a link or allowing a later confirm/payment step.
        payment_unavailable = (
            source.get("payment_method_available") is False
            or source.get("payment_method_unavailable") is True
            or extra.get("paymentMethodAvailable") is False
            or extra.get("payment_method_available") is False
        )
        if payment_unavailable:
            return fail(
                f"{label} 支付方式不可用，已阻止继续支付",
                f"{mode or 'engine'}_payment_method_unavailable",
                f"{label} 支付方式不可用，已阻止继续支付",
            )

        if mode == "paypal":
            link = _text(source.get("ba_url"))
            if link:
                result["ok"] = True
                result["link"] = link
                result["error"] = None
                return result
            return fail("PayPal 未生成有效 BA 链接", "paypal_link_missing")

        if mode == "blik":
            link = _text(source.get("link")) or _text(source.get("checkout_url"))
            paid = source.get("paid") is True and _text(source.get("payment_status")) == "plus_active"
            if link and paid:
                result["ok"] = True
                result["link"] = link
                result["error"] = None
                return result
            if link:
                return fail("BLIK 未确认为 Plus 已到账，已阻止输出支付链接", "blik_payment_not_verified", "BLIK 未确认 Plus 到账，已阻止输出支付链接")
            return fail("BLIK 未生成有效支付链接", "blik_link_missing")

        if mode == "upi":
            hosted = (
                _text(source.get("hosted_instructions_url"))
                or _text(source.get("hostedInstructionsUrl"))
                or _text(source.get("link"))
            )
            link = hosted if _stripe_upi_hosted_instructions(hosted) and not _stripe_qr_image(hosted) else ""
            if not link:
                return fail("UPI 未生成 Stripe hosted instructions URL", "upi_link_missing")
            amount = _first_present(source, "checkout_amount", "amount")
            zero_verified = _is_zero(amount) and source.get("promo_applied") is True
            # A force flag only controls the upstream engine's strategy.  It
            # must never turn an unverified/non-zero checkout into a success;
            # the adapter is the final authority before confirm/payment.
            if zero_verified:
                result["ok"] = True
                result["link"] = link
                result["error"] = None
                return result
            return fail(
                "UPI 未确认优惠后应付金额为 0 且优惠已生效，已阻止输出支付链接",
                "upi_zero_amount_not_confirmed",
                "UPI 优惠未应用：最终应付金额未明确压到 0，已阻止输出支付链接",
            )

        if mode == "ideal":
            link = _text(source.get("link")) or _text(source.get("provider_redirect_url")) or _text(source.get("url"))
            zero_verified = _is_zero(source.get("checkout_amount")) and source.get("promo_applied") is True
            if link and zero_verified:
                result["ok"] = True
                result["link"] = link
                result["error"] = None
                return result
            if link:
                return fail(
                    "ideal 未确认优惠后应付金额为 0 且优惠已生效，已阻止输出支付链接",
                    "ideal_zero_amount_not_confirmed",
                    "ideal 未确认优惠后应付金额为 0，已阻止输出支付链接",
                )
            return fail("ideal 未生成有效支付链接", "ideal_link_missing")

        if mode == "pix":
            link = _text(source.get("provider_redirect_url")) or _text(source.get("link")) or _text(source.get("url"))
            if link:
                result["ok"] = True
                result["link"] = link
                result["error"] = None
                return result
            return fail("PIX 未生成有效支付链接", "pix_link_missing")

        if mode in PAYMENT_MODES:
            link = _text(source.get("link"))
            if link:
                result["ok"] = True
                result["link"] = link
                result["error"] = None
                return result
            return fail(f"{label} 未生成有效支付链接", f"{mode}_link_missing")

        return fail(f"{label} 提链失败", f"{mode or 'engine'}_engine_failed")

    async def execute(
        self,
        mode: str,
        at: str,
        email: str,
        proxy: str,
        payload: dict[str, Any],
        exit_proxy: str = "",
        *,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        read_control: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        command = self.command(mode, at, email, proxy, payload, exit_proxy)
        params = _params(payload)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(ROOT),
            env=self.environment(mode, payload),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2 * 1024 * 1024,
            start_new_session=os.name != "nt",
        )
        stages: list[dict[str, str]] = []
        final: dict[str, Any] | None = None
        stderr_tail = ""
        completed = asyncio.Event()
        canceled = False
        latest_qr: dict[str, Any] = {}

        def awaiting_initial_qr(message: dict[str, Any]) -> bool:
            extra = public_engine_extra(message)
            return message.get("ok") is True and bool(extra.get("qrPending")) and not bool(extra.get("qrImage"))

        def with_ready_qr(message: dict[str, Any], qr: dict[str, Any]) -> dict[str, Any]:
            return {**message, "qr_image": public_engine_extra(qr)["qrImage"], "qr_pending": False,
                    "extra": {**(message.get("extra") or {}), **public_engine_extra(qr), "qrPending": False, "qr_pending": False}}

        async def read_stdout() -> None:
            nonlocal final
            assert process.stdout
            while line := await process.stdout.readline():
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if text.startswith("[") and "]" in text:
                    parts = text.split("]", 2)
                    stage = parts[0].lstrip("[").removeprefix(mode + ":")
                    status = parts[1].strip().lstrip("[") if len(parts) > 1 else ""
                    detail = parts[2].strip() if len(parts) > 2 else ""
                    stages.append({"stage": stage[-64:], "status": status[-32:], "detail": detail[:500]})
                    stages[:] = stages[-50:]
                    if on_event:
                        await on_event({"event": "stage", **stages[-1]})
                if text.startswith("{") and text.endswith("}"):
                    try:
                        message = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get("event"):
                        if on_event:
                            await on_event(message)
                        if message.get("event") == "qr_refresh" and public_engine_extra(message).get("qrImage"):
                            latest_qr.update(message)
                        if final is not None and awaiting_initial_qr(final) and message.get("event") == "qr_refresh":
                            image = public_engine_extra(message).get("qrImage")
                            if image:
                                final = with_ready_qr(final, message)
                                completed.set()
                    elif isinstance(message.get("ok"), bool) and final is None:
                        final = message
                        if awaiting_initial_qr(final) and latest_qr:
                            final = with_ready_qr(final, latest_qr)
                        if awaiting_initial_qr(final):
                            if on_event:
                                await on_event({**final, "event": "result_pending"})
                        else:
                            completed.set()

        async def read_stderr() -> None:
            nonlocal stderr_tail
            assert process.stderr
            while chunk := await process.stderr.read(4096):
                stderr_tail = (stderr_tail + chunk.decode("utf-8", "replace"))[-4000:]

        async def forward_controls() -> None:
            nonlocal canceled
            while read_control and not completed.is_set():
                message = await read_control()
                if message and message.get("command") == "cancel":
                    canceled = True
                    completed.set()
                    return
                if message and process.stdin and not process.stdin.is_closing():
                    process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
                    await process.stdin.drain()
                await asyncio.sleep(0.3)

        async def stop_process() -> None:
            if process.returncode is not None:
                return
            if os.name == "nt":
                # The engine result is terminal as soon as a usable QR/link is
                # emitted.  Terminate the direct Python process first; invoking
                # taskkill /T for every GCash watcher can take several seconds
                # while it walks a process tree, which made an already complete
                # result look as though it was waiting for the QR watcher.
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()

        readers = [asyncio.create_task(read_stdout()), asyncio.create_task(read_stderr())]
        process_wait = asyncio.create_task(process.wait())
        result_wait = asyncio.create_task(completed.wait())
        control_task = asyncio.create_task(forward_controls()) if read_control else None
        timed_out = False

        try:
            # Result JSON completes extraction; background QR threads do not.
            pending = {process_wait, result_wait, readers[0]}
            if control_task:
                pending.add(control_task)
            done, _ = await asyncio.wait(pending, timeout=self.timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
            timed_out = not done
            for task in done:
                task.result()
        finally:
            if control_task:
                control_task.cancel()
            result_wait.cancel()
            await stop_process()
            await asyncio.gather(*readers, process_wait, result_wait, *([control_task] if control_task else []), return_exceptions=True)
        if canceled:
            return {"ok": False, "error": "任务被取消", "reason_code": "engine_canceled", "retryable": False}, stages
        if timed_out:
            return {"ok": False, "error": "执行超时", "reason_code": "engine_timeout", "retryable": True}, stages
        if final:
            if awaiting_initial_qr(final):
                return {"ok": False, "error": "支付链接已返回，但二维码会话在首张二维码就绪前结束", "reason_code": "engine_qr_incomplete", "retryable": True}, stages
            result = self._normalize_result(mode, final, params)
            result["extra"] = {**result.get("extra", {}), "qrRefreshActive": False, "qr_refresh_active": False, "qrPending": False, "qr_pending": False}
            return result, stages

        missing_module = re.search(
            r"(?:ModuleNotFoundError|ImportError):\s*No module named ['\"]([^'\"]+)['\"]",
            stderr_tail,
            flags=re.IGNORECASE,
        )
        if missing_module:
            module_name = missing_module.group(1)
            detail = f"提链引擎运行依赖缺失：{module_name}。请在 backend/.venv 中安装项目依赖后重试"
            stages.append({"stage": "engine_bootstrap", "status": "fail", "detail": detail})
            return {
                "ok": False,
                "error": detail,
                "reason_code": "engine_dependency_missing",
                "retryable": False,
                "extra": {"missing_module": module_name, "stderr": stderr_tail[-1000:]},
            }, stages
        return {
            "ok": False,
            "error": (stderr_tail.strip() or f"引擎未返回结果，退出码 {process.returncode}")[-1000:],
            "reason_code": "engine_no_result",
            "retryable": True,
        }, stages
