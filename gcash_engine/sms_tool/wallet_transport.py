"""HTTP transport for the shared GoPay, GCash, and GrabPay wallet adapter."""

from __future__ import annotations

import json
import re
import threading
import uuid
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

from .checkout_contract import CHECKOUT_PATH, CHECKOUT_URL, STRIPE_INIT_URL
from .wallet_provider import WALLET_METHODS, WalletProviderError, WalletTransportRequest, redact_sensitive_text
from .paypal_proxy import is_retryable_network_error, normalize_proxy_url


PAYMENT_METHOD_URL = "https://api.stripe.com/v1/payment_methods"
APPROVE_PATH = "/backend-api/payments/checkout/approve"
APPROVE_URL = f"https://chatgpt.com{APPROVE_PATH}"
SENTINEL_PATH = "/backend-api/sentinel/ping"
SENTINEL_URL = f"https://chatgpt.com{SENTINEL_PATH}"
PROMOTION_PATH = "/backend-api/payments/checkout/update"
PROMOTION_URL = f"https://chatgpt.com{PROMOTION_PATH}"
_STRIPE_PAGE_URL = "https://api.stripe.com/v1/payment_pages/{checkout_session_id}"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BASE_REDIRECT_HOSTS = frozenset({
    "pm-redirects.stripe.com",
    "hooks.stripe.com",
    "checkout.stripe.com",
    "pay.openai.com",
    "chatgpt.com",
})
_HTML_REDIRECT_RE = re.compile(
    r"(?i)(?:url\s*=\s*|window\.location(?:\.href)?\s*=\s*)[\"']?"
    r"(https://[^\"'<>\s]+)"
)
_CHATGPT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 "
    "Safari/537.36"
)
_CHATGPT_CLIENT_VERSION = "prod-db390ebea64862bf1899c420a4c736e0cf639747"
_CHATGPT_CLIENT_BUILD = "7904904"


class WalletHTTPError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0) -> None:
        self.status_code = int(status_code or 0)
        super().__init__(message)


class ChatGPTStripeWalletTransport:
    """Production wire transport with stage-specific proxy routing."""

    def __init__(
        self,
        *,
        timeout: int = 45,
        max_redirect_hops: int = 8,
        chatgpt_session_factory: Callable[[WalletTransportRequest, str], Any] | None = None,
        fingerprint: Mapping[str, Any] | None = None,
    ) -> None:
        self.timeout = max(5, int(timeout or 45))
        # 账号指纹档案（按建单国家分配；None = 引擎内置默认指纹）
        self._fingerprint = dict(fingerprint) if isinstance(fingerprint, Mapping) else None
        # 请求时发现 TLS 指纹目标不被当前 curl_cffi 支持（ImpersonateError）
        # 的 flow 集合：这些 flow 的会话回退到默认 chrome136 重建
        self._impersonate_fallback_flows: set[str] = set()
        self.max_redirect_hops = max(1, min(int(max_redirect_hops or 8), 12))
        self._sessions: dict[tuple[str, str], Any] = {}
        self._chatgpt_sessions: dict[tuple[str, str], Any] = {}
        self._checkout_proxies: dict[str, str] = {}
        self._chatgpt_session_ids: dict[str, str] = {}
        self._checkout_cookies: dict[str, str] = {}
        self._chatgpt_session_factory = chatgpt_session_factory
        self._lock = threading.Lock()

    def create_checkout(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        def send(proxy: str) -> Mapping[str, Any]:
            with self._lock:
                self._checkout_proxies[request.flow_id] = normalize_proxy_url(proxy)
            session = self._chatgpt_session(request, proxy)
            response = session.post(
                CHECKOUT_URL,
                json=dict(request.payload),
                headers=self._chatgpt_headers(request, CHECKOUT_PATH),
                timeout=self.timeout,
            )
            self._remember_response_cookies(request.flow_id, response)
            self._sync_session_cookies(request.flow_id, session)
            self._refresh_chatgpt_cookie_header(request, session)
            return self._json_response(response, request.stage)

        try:
            return self._with_proxy_rotation(request, send)
        except WalletProviderError:
            raise
        except WalletHTTPError:
            raise
        except Exception as exc:
            raise WalletHTTPError(f"checkout transport failed: {type(exc).__name__}") from exc

    def update_checkout_promotion(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        referer = f"https://chatgpt.com/checkout/{request.processor_entity}/{request.checkout_session_id}"

        def send(proxy: str) -> Mapping[str, Any]:
            checkout_proxy = self._checkout_stage_proxy(request)
            checkout_session = self._chatgpt_session(request, checkout_proxy)
            session = checkout_session
            owns_session = False
            if normalize_proxy_url(proxy) != normalize_proxy_url(checkout_proxy):
                session = self._clone_chatgpt_session(checkout_session, proxy)
                owns_session = True
            try:
                response = session.post(
                    PROMOTION_URL,
                    json=dict(request.payload),
                    headers=self._chatgpt_headers(request, PROMOTION_PATH, referer=referer),
                    timeout=self.timeout,
                )
                self._remember_response_cookies(request.flow_id, response)
                self._sync_chatgpt_cookies(checkout_session, session)
                self._sync_session_cookies(request.flow_id, checkout_session)
                self._refresh_chatgpt_cookie_header(request, checkout_session)
                return self._json_response(response, request.stage)
            finally:
                if owns_session:
                    _safe_close(session)

        try:
            return self._with_proxy_rotation(request, send)
        except WalletProviderError:
            raise
        except WalletHTTPError:
            raise
        except Exception as exc:
            raise WalletHTTPError(f"promotion transport failed: {type(exc).__name__}") from exc

    def read_checkout_page(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        url = f"https://chatgpt.com/checkout/{request.processor_entity}/{request.checkout_session_id}"

        def read(proxy: str) -> Mapping[str, Any]:
            checkout_proxy = self._checkout_stage_proxy(request)
            checkout_session = self._chatgpt_session(request, checkout_proxy)
            session = checkout_session
            owns_session = False
            if normalize_proxy_url(proxy) != normalize_proxy_url(checkout_proxy):
                session = self._clone_chatgpt_session(checkout_session, proxy)
                owns_session = True
            try:
                response = session.get(
                    url,
                    headers=self._chatgpt_headers(request, "", accept="text/html,application/xhtml+xml"),
                    timeout=self.timeout,
                )
                self._remember_response_cookies(request.flow_id, response)
                self._sync_session_cookies(request.flow_id, checkout_session)
                self._refresh_chatgpt_cookie_header(request, checkout_session)
                status_code = int(getattr(response, "status_code", 0) or 0)
                if status_code >= 400:
                    raise WalletHTTPError(f"checkout page returned HTTP {status_code}", status_code)
                return {"html": str(getattr(response, "text", "") or "")}
            finally:
                if owns_session:
                    _safe_close(session)

        try:
            return self._with_proxy_rotation(request, read)
        except WalletProviderError:
            raise
        except WalletHTTPError:
            raise
        except Exception as exc:
            raise WalletHTTPError(f"checkout page transport failed: {type(exc).__name__}") from exc

    def extract_provider_link(self, request: WalletTransportRequest) -> Mapping[str, Any] | str:
        from .gopay_browser import extract_gopay_midtrans_url

        checkout_url = str(request.redirect_url or request.payload.get("checkout_url") or "").strip()
        if not checkout_url:
            raise WalletProviderError(
                "GoPay checkout URL is missing for browser extraction",
                error_code="gopay_checkout_url_missing",
                error_stage="extract_link",
                retryable=True,
            )
        context = request.transport_context
        headless_value = context.get("gopay_browser_headless", False)
        browser_engine = str(context.get("gopay_browser_engine") or "auto").strip()
        try:
            headless = True if headless_value in (None, "") else bool(headless_value)
            if isinstance(headless_value, str):
                headless = headless_value.strip().lower() in {"1", "true", "yes", "on"}
            errors: list[str] = []
            url = ""
            for candidate_proxy in self._extract_link_proxy_candidates(request):
                try:
                    url = extract_gopay_midtrans_url(
                        checkout_url,
                        proxy=candidate_proxy,
                        cookie_header=self._cookie_header(request),
                        timeout_seconds=int(context.get("gopay_browser_timeout_seconds") or self.timeout),
                        headless=headless,
                        browser_engine=browser_engine,
                        cdp_url=str(context.get("gopay_browser_cdp_url") or ""),
                        billing_details=request.payload.get("billing_details")
                        if isinstance(request.payload.get("billing_details"), Mapping)
                        else None,
                    )
                    if url:
                        break
                except Exception as proxy_exc:
                    errors.append(redact_sensitive_text(proxy_exc))
            if not url:
                raise RuntimeError("; ".join(errors) or "no proxy candidate returned a GoPay URL")
        except Exception as exc:
            raise WalletProviderError(
                f"GoPay browser extraction failed: {type(exc).__name__}: {redact_sensitive_text(exc)}",
                error_code="gopay_browser_extraction_failed",
                error_stage="extract_link",
                retryable=True,
            ) from exc
        return {"provider_redirect_url": url, "final_url": url}

    def _extract_link_proxy_candidates(self, request: WalletTransportRequest) -> list[str]:
        primary = self._stage_proxy(request)
        context = request.transport_context
        raw_pool = context.get("proxy_pool") if isinstance(context.get("proxy_pool"), list) else []
        limit = context.get("gopay_browser_proxy_attempts") or 4
        try:
            max_candidates = max(1, min(int(limit), 20))
        except (TypeError, ValueError):
            max_candidates = 4
        candidates: list[str] = []
        for value in [primary, *raw_pool]:
            proxy = normalize_proxy_url(str(value or "").strip())
            if proxy and proxy not in candidates:
                candidates.append(proxy)
            if len(candidates) >= max_candidates:
                break
        return candidates or [""]

    def _cookie_header(self, request: WalletTransportRequest) -> str:
        base = str(request.auth_context.get("cookie_header") or "").strip()
        with self._lock:
            extra = self._checkout_cookies.get(request.flow_id, "")
        if not extra:
            return base
        pairs: dict[str, str] = {}
        for source in (base, extra):
            for item in source.split(";"):
                name, sep, value = item.strip().partition("=")
                if sep and name:
                    pairs[name.strip()] = value.strip()
        return "; ".join(f"{name}={value}" for name, value in pairs.items())

    def _chatgpt_session(self, request: WalletTransportRequest, proxy_override: str = "") -> Any:
        """Return the persistent ChatGPT session for this checkout flow."""
        proxy = normalize_proxy_url(proxy_override) if proxy_override else self._checkout_stage_proxy(request)
        key = (request.flow_id, proxy)
        with self._lock:
            session = self._chatgpt_sessions.get(key)
            if session is None:
                if self._chatgpt_session_factory is not None:
                    session = self._chatgpt_session_factory(request, proxy)
                else:
                    fallback_fp = None
                    if request.flow_id in self._impersonate_fallback_flows:
                        # ImpersonateError 兜底：强制默认 chrome136 TLS 指纹
                        fallback_fp = {"impersonate": "chrome136",
                                       "ua": _CHATGPT_USER_AGENT,
                                       "accept_language": _request_accept_language(request)}
                    session = _new_chatgpt_session(request, proxy, self._chatgpt_session_ids,
                                                   fingerprint=fallback_fp or self._fingerprint)
                self._chatgpt_sessions[key] = session
        self._sync_session_cookies(request.flow_id, session)
        return session

    def _clone_chatgpt_session(self, source: Any, proxy: str) -> Any:
        impersonate = None
        if self._fingerprint and self._fingerprint.get("impersonate"):
            impersonate = self._fingerprint["impersonate"]
        session = _clone_http_session(source, proxy, impersonate=impersonate)
        return session

    def _checkout_stage_proxy(self, request: WalletTransportRequest) -> str:
        with self._lock:
            remembered = self._checkout_proxies.get(request.flow_id, "")
        return remembered or self._stage_proxy(request, stage="checkout")

    def _chatgpt_headers(
        self,
        request: WalletTransportRequest,
        target_path: str,
        *,
        referer: str = "https://chatgpt.com/",
        accept: str = "*/*",
    ) -> dict[str, str]:
        device_id = _request_device_id(request)
        fp = self._fingerprint or {}
        sec_ch_ua = str(fp.get("sec_ch_ua") or "").strip()
        session_id = str(fp.get("oai_session_id") or "").strip() or self._chatgpt_session_id(request.flow_id)
        headers = {
            "User-Agent": str(fp.get("ua") or _CHATGPT_USER_AGENT),
            "Accept": accept,
            "Accept-Language": str(fp.get("accept_language") or _request_accept_language(request)),
            "Authorization": f"Bearer {request.access_token}",
            "Origin": "https://chatgpt.com",
            "Referer": referer,
            "Content-Type": "application/json",
            "oai-device-id": device_id,
            # The direct-card extractor uses the browser's ChatGPT locale for
            # this header even when the billing currency is localized.
            "oai-language": str(fp.get("oai_language") or "en-US"),
            # oai-session-id 与设备绑定：指纹档案提供则全程不变（含跨任务）
            "oai-session-id": session_id,
            "oai-client-version": _CHATGPT_CLIENT_VERSION,
            "oai-client-build-number": _CHATGPT_CLIENT_BUILD,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if sec_ch_ua:
            # Safari / Firefox 无 sec-ch-ua 家族，仅 chromium 内核携带
            headers["sec-ch-ua"] = sec_ch_ua
            headers["sec-ch-ua-mobile"] = str(fp.get("sec_ch_ua_mobile") or "?0")
            headers["sec-ch-ua-platform"] = str(fp.get("sec_ch_ua_platform") or '"Windows"')
        if target_path:
            headers["x-openai-target-path"] = target_path
            headers["x-openai-target-route"] = target_path
        account_id = str(
            request.auth_context.get("chatgpt_account_id")
            or request.auth_context.get("account_id")
            or ""
        ).strip()
        if account_id:
            headers["Chatgpt-Account-Id"] = account_id
        cookie_header = _chatgpt_cookie_header(
            request,
            device_id,
            base_cookie=self._cookie_header(request),
        )
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    def _chatgpt_session_id(self, flow_id: str) -> str:
        with self._lock:
            value = self._chatgpt_session_ids.get(flow_id)
            if not value:
                value = str(uuid.uuid4())
                self._chatgpt_session_ids[flow_id] = value
            return value

    def _sync_chatgpt_cookies(self, target: Any, source: Any) -> None:
        _sync_session_cookies(target, source)
        target_headers = getattr(target, "headers", None)
        if target_headers is not None:
            target_headers["Cookie"] = _merge_cookie_headers(
                str(target_headers.get("Cookie") or ""),
                str(getattr(source, "headers", {}).get("Cookie") or ""),
                self._cookie_header_for_session(source),
            )

    @staticmethod
    def _cookie_header_for_session(session: Any) -> str:
        try:
            return "; ".join(
                f"{name}={value}"
                for name, value in session.cookies.items()
                if name and value is not None
            )
        except Exception:
            return ""

    def _sync_session_cookies(self, flow_id: str, session: Any) -> None:
        """Keep the serialized cookie view aligned with the live cookie jar."""
        cookies = getattr(session, "cookies", None)
        if cookies is None:
            return
        try:
            values = {str(name): str(value) for name, value in cookies.items() if name}
        except Exception:
            values = {}
        if not values:
            return
        class _CookieResponse:
            pass
        response = _CookieResponse()
        response.cookies = values
        self._remember_response_cookies(flow_id, response)

    def _refresh_chatgpt_cookie_header(
        self,
        request: WalletTransportRequest,
        session: Any,
    ) -> None:
        headers = getattr(session, "headers", None)
        if headers is None:
            return
        cookie_header = _merge_cookie_headers(
            self._cookie_header(request),
            self._cookie_header_for_session(session),
        )
        if cookie_header:
            headers["Cookie"] = cookie_header

    def _remember_response_cookies(self, flow_id: str, response: Any) -> None:
        cookies: dict[str, str] = {}
        jar = getattr(response, "cookies", None)
        if jar is not None:
            try:
                for name, value in jar.items():
                    if name and value is not None:
                        cookies[str(name)] = str(value)
            except Exception:
                pass
        if not cookies:
            return
        with self._lock:
            existing = self._checkout_cookies.get(str(flow_id), "")
            merged: dict[str, str] = {}
            for item in existing.split(";"):
                name, sep, value = item.strip().partition("=")
                if sep and name:
                    merged[name.strip()] = value.strip()
            merged.update(cookies)
            self._checkout_cookies[str(flow_id)] = "; ".join(
                f"{name}={value}" for name, value in merged.items()
            )

    def stripe_init(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        def init(proxy: str) -> Mapping[str, Any]:
            response = self._session(request, proxy).post(
                STRIPE_INIT_URL.format(checkout_session_id=request.checkout_session_id),
                data=dict(request.payload),
                headers=self._stripe_headers(request),
                timeout=self.timeout,
            )
            return self._json_response(response, request.stage)

        return self._with_proxy_rotation(request, init)

    def create_payment_method(self, request: WalletTransportRequest) -> Mapping[str, Any] | str:
        def create(proxy: str) -> Mapping[str, Any] | str:
            response = self._session(request, proxy).post(
                PAYMENT_METHOD_URL,
                data=dict(request.payload),
                headers=self._stripe_headers(request),
                timeout=self.timeout,
            )
            return self._json_response(response, request.stage)

        return self._with_proxy_rotation(request, create)

    def confirm_payment(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        response = self._session(request).post(
            _STRIPE_PAGE_URL.format(checkout_session_id=request.checkout_session_id) + "/confirm",
            data=dict(request.payload),
            headers=self._stripe_headers(request),
            timeout=self.timeout,
        )
        return self._json_response(response, request.stage)

    def approve_checkout(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        from . import gen_pp_link

        device_id = str(
            request.auth_context.get("device_id")
            or request.auth_context.get("oai_device_id")
            or request.flow_id
            or uuid.uuid4()
        ).strip()
        cookie_header = _chatgpt_cookie_header(request, device_id)
        account_id = str(
            request.auth_context.get("chatgpt_account_id")
            or request.auth_context.get("account_id")
            or ""
        ).strip()
        proxy = self._stage_proxy(request)
        referer = f"https://chatgpt.com/checkout/{request.processor_entity}/{request.checkout_session_id}"
        extra_headers = {
            **_chatgpt_device_headers(request, device_id),
            "Referer": referer,
            "x-openai-target-path": APPROVE_PATH,
            "x-openai-target-route": APPROVE_PATH,
        }
        if account_id:
            extra_headers["Chatgpt-Account-Id"] = account_id
        try:
            sentinel_headers = {
                **_chatgpt_device_headers(request, device_id),
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": SENTINEL_PATH,
                "x-openai-target-route": SENTINEL_PATH,
            }
            if account_id:
                sentinel_headers["Chatgpt-Account-Id"] = account_id
            try:
                gen_pp_link._checkout_post(
                    SENTINEL_URL,
                    {},
                    request.access_token,
                    cookie_header,
                    proxy,
                    self.timeout,
                    extra_headers=sentinel_headers,
                )
            except Exception:
                pass
            response = gen_pp_link._checkout_post(
                APPROVE_URL,
                dict(request.payload),
                request.access_token,
                cookie_header,
                proxy,
                self.timeout,
                extra_headers=extra_headers,
            )
        except Exception as exc:
            raise WalletHTTPError(f"approve transport failed: {type(exc).__name__}") from exc
        return self._json_response(response, request.stage)

    def poll_payment(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        response = self._session(request).get(
            _STRIPE_PAGE_URL.format(checkout_session_id=request.checkout_session_id),
            params=dict(request.payload),
            headers=self._stripe_headers(request),
            timeout=self.timeout,
        )
        return self._json_response(response, request.stage)

    def extract_intent_redirect(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        reference = _find_intent_reference(request.payload.get("payment_page_response"))
        if not reference:
            return {}
        intent_id, client_secret, intent_path = reference
        if not request.publishable_key:
            return {}
        response = self._session(request).get(
            f"https://api.stripe.com/v1/{intent_path}/{intent_id}",
            params={
                "key": request.publishable_key,
                "client_secret": client_secret,
            },
            headers=self._stripe_headers(request),
            timeout=self.timeout,
        )
        return self._json_response(response, "stripe_intent")

    def follow_redirect(self, request: WalletTransportRequest) -> Mapping[str, Any] | str:
        current = str(request.redirect_url or "").strip()
        if not current:
            raise WalletProviderError(
                "wallet redirect URL is missing",
                error_code="wallet_redirect_missing",
                error_stage="follow_redirect",
            )
        session = self._session(request)
        seen: set[str] = set()
        for _ in range(self.max_redirect_hops):
            self._validate_redirect_url(current, request.method)
            fingerprint = _url_fingerprint(current)
            if fingerprint in seen:
                raise WalletProviderError(
                    "wallet redirect loop detected",
                    error_code="wallet_redirect_loop",
                    error_stage="follow_redirect",
                )
            seen.add(fingerprint)
            response = session.get(
                current,
                headers={
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Referer": "https://checkout.stripe.com/",
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in _REDIRECT_STATUSES:
                location = str((getattr(response, "headers", {}) or {}).get("Location") or "").strip()
                if not location:
                    raise WalletHTTPError("wallet redirect response omitted Location", status_code)
                current = urljoin(current, location)
                continue
            if status_code >= 400:
                raise WalletHTTPError(f"follow_redirect returned HTTP {status_code}", status_code)
            body = str(getattr(response, "text", "") or "")[:20000]
            candidate = _html_redirect(body)
            if candidate:
                current = urljoin(current, candidate)
                continue
            final_url = str(getattr(response, "url", "") or current).strip()
            self._validate_redirect_url(final_url, request.method)
            return {"final_url": final_url}
        raise WalletProviderError(
            "wallet redirect exceeded the hop limit",
            error_code="wallet_redirect_hop_limit",
            error_stage="follow_redirect",
            retryable=False,
            status="unknown",
        )

    def _with_proxy_rotation(
        self,
        request: WalletTransportRequest,
        operation: Callable[[str], Any],
    ) -> Any:
        candidates = self._stage_proxy_candidates(request)
        last_error: Exception | None = None
        for index, proxy in enumerate(candidates):
            try:
                return operation(proxy)
            except Exception as exc:
                last_error = exc
                # TLS 指纹目标不被当前 curl_cffi 支持（如 firefox136）：
                # 标记该 flow 回退默认 chrome136 重建会话，重试一次
                if self._is_impersonate_error(exc):
                    with self._lock:
                        self._impersonate_fallback_flows.add(request.flow_id)
                        self._chatgpt_sessions.pop((request.flow_id, proxy), None)
                        self._sessions.pop((request.flow_id, proxy), None)
                    try:
                        return operation(proxy)
                    except Exception as exc2:
                        last_error = exc2
                        self._maybe_revert_impersonate_fallback(request)
                if (
                    index + 1 >= len(candidates)
                    or not self._should_rotate_proxy(request, exc)
                ):
                    raise
        if last_error is not None:
            raise last_error
        return operation("")

    @staticmethod
    def _is_impersonate_error(exc: Exception) -> bool:
        name = type(exc).__name__
        text = str(exc).lower()
        return "impersonate" in name.lower() or "impersonat" in text or "not supported" in text and "curl" in text

    def _maybe_revert_impersonate_fallback(self, request: WalletTransportRequest) -> None:
        """重试成功（返回前）才移除 fallback 标记；失败保留（下次尝试仍用 chrome136）。"""
        with self._lock:
            self._impersonate_fallback_flows.discard(request.flow_id)

    def _should_rotate_proxy(self, request: WalletTransportRequest, error: Exception) -> bool:
        """Rotate only for transport/proxy failures, never for auth or eligibility errors."""
        if request.stage in {"confirm", "approve", "poll", "follow_redirect"}:
            return False
        status_code = int(getattr(error, "status_code", 0) or 0)
        if status_code in {408, 425, 429} or status_code >= 500:
            return True
        if is_retryable_network_error(error):
            return True
        return bool(getattr(error, "retryable", False))

    def _stage_proxy_candidates(self, request: WalletTransportRequest) -> list[str]:
        context = request.transport_context
        raw_pool = context.get("proxy_pool") if isinstance(context.get("proxy_pool"), list) else []
        limit = context.get("proxy_rotation_attempts") or 6
        try:
            max_candidates = max(1, min(int(limit), 20))
        except (TypeError, ValueError):
            max_candidates = 6
        values = [self._stage_proxy(request), *raw_pool]
        candidates: list[str] = []
        for value in values:
            proxy = normalize_proxy_url(str(value or "").strip())
            if proxy and proxy not in candidates:
                candidates.append(proxy)
            if len(candidates) >= max_candidates:
                break
        return candidates or [""]

    def _session(self, request: WalletTransportRequest, proxy_override: str = "") -> Any:
        from . import gen_pp_link

        proxy = normalize_proxy_url(proxy_override) if proxy_override else self._stage_proxy(request)
        key = (request.flow_id, proxy)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = gen_pp_link._new_session(proxy, user_agent=(self._fingerprint or {}).get("ua") or "")
                self._sessions[key] = session
        return session

    def _stage_proxy(self, request: WalletTransportRequest, *, stage: str | None = None) -> str:
        context = request.transport_context
        current_stage = stage or request.stage
        stage_keys = {
            "checkout": ("checkout_proxy",),
            "promotion": ("promotion_proxy", "checkout_proxy"),
            "checkout_page": ("checkout_proxy", "promotion_proxy"),
            "extract_link": ("provider_proxy", "redirect_proxy", "checkout_proxy"),
            "stripe_init": ("stripe_init_proxy", "provider_proxy"),
            "payment_method": ("payment_method_proxy", "provider_proxy"),
            "confirm": ("confirm_proxy", "provider_proxy"),
            "approve": ("approve_proxy", "checkout_proxy"),
            "poll": ("provider_proxy", "stripe_init_proxy"),
            "follow_redirect": ("redirect_proxy", "provider_proxy"),
        }
        for key in (*stage_keys.get(current_stage, ()), "proxy", "default_proxy"):
            value = context.get(key)
            if isinstance(value, Mapping):
                value = value.get("https") or value.get("http") or ""
            normalized = normalize_proxy_url(str(value or "").strip())
            if normalized:
                return normalized
        return ""

    def _stripe_headers(self, request: WalletTransportRequest) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://checkout.stripe.com",
            "Referer": f"https://checkout.stripe.com/c/pay/{request.checkout_session_id}",
        }
        fp = self._fingerprint or {}
        if fp.get("ua"):
            headers["User-Agent"] = str(fp["ua"])
        return headers

    @staticmethod
    def _json_response(response: Any, stage: str) -> Mapping[str, Any]:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code >= 400:
            if status_code == 401:
                raise WalletProviderError(
                    f"{stage} authentication failed (HTTP 401)",
                    error_code="access_token_invalid_or_expired",
                    error_stage=stage,
                    retryable=False,
                )
            if status_code == 403 and _is_cloudflare_response(response):
                raise WalletProviderError(
                    f"{stage} blocked by Cloudflare HTTP 403",
                    error_code=f"{stage}_cloudflare_blocked",
                    error_stage=stage,
                    retryable=True,
                    status="failed",
                )
            raise WalletHTTPError(
                f"{stage} returned HTTP {status_code}: {_response_error_summary(response)}",
                status_code,
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise WalletHTTPError(f"{stage} returned invalid JSON", status_code) from exc
        if not isinstance(payload, Mapping):
            raise WalletHTTPError(f"{stage} returned a non-object JSON payload", status_code)
        return payload

    @staticmethod
    def _validate_redirect_url(url: str, method: str) -> None:
        try:
            parsed = urlsplit(str(url or "").strip())
        except Exception as exc:
            raise WalletProviderError(
                "wallet redirect URL is invalid",
                error_code="wallet_redirect_invalid",
                error_stage="follow_redirect",
            ) from exc
        host = str(parsed.hostname or "").lower().rstrip(".")
        spec = WALLET_METHODS.get(str(method or "").lower())
        allowed = _BASE_REDIRECT_HOSTS | frozenset(spec.redirect_hosts if spec else ())
        if parsed.scheme.lower() != "https" or parsed.username or parsed.password or not any(
            host == suffix or host.endswith(f".{suffix}") for suffix in allowed
        ):
            raise WalletProviderError(
                "wallet redirect host is not allowed",
                error_code="wallet_redirect_host_not_allowed",
                error_stage="follow_redirect",
            )


def _html_redirect(body: str) -> str:
    match = _HTML_REDIRECT_RE.search(str(body or "").replace("&amp;", "&"))
    return match.group(1) if match else ""


def _chatgpt_device_headers(request: WalletTransportRequest, device_id: str, fingerprint: Mapping[str, Any] | None = None) -> dict[str, str]:
    fp = fingerprint if isinstance(fingerprint, Mapping) else {}
    locale = str(request.contract.browser_locale or "id-ID").strip()
    # oai-session-id 与设备绑定：指纹档案提供则全程不变（含跨任务）
    session_id = str(fp.get("oai_session_id") or "").strip() or device_id
    return {
        "Origin": "https://chatgpt.com",
        "Accept": "*/*",
        "Accept-Language": str(fp.get("accept_language") or f"{locale},{locale.split('-')[0]};q=0.9,en;q=0.8"),
        "oai-device-id": device_id,
        "oai-session-id": session_id,
        "oai-language": str(fp.get("oai_language") or "en-US"),
        "oai-client-version": "prod-db390ebea64862bf1899c420a4c736e0cf639747",
        "oai-client-build-number": "7904904",
    }


def _request_device_id(request: WalletTransportRequest) -> str:
    value = str(
        request.auth_context.get("device_id")
        or request.auth_context.get("oai_device_id")
        or ""
    ).strip()
    if value:
        return value
    cookie_header = str(request.auth_context.get("cookie_header") or "")
    for item in cookie_header.split(";"):
        name, separator, cookie_value = item.strip().partition("=")
        if separator and name.strip() == "oai-did" and cookie_value.strip():
            return cookie_value.strip()
    return request.flow_id or str(uuid.uuid4())


def _request_accept_language(request: WalletTransportRequest) -> str:
    locale = str(request.contract.browser_locale or "id-ID").strip()
    language = locale.split("-", 1)[0]
    return f"{locale},{language};q=0.9,en;q=0.8"


def _new_chatgpt_session(
    request: WalletTransportRequest,
    proxy: str,
    session_ids: dict[str, str],
    fingerprint: Mapping[str, Any] | None = None,
) -> Any:
    fp = fingerprint if isinstance(fingerprint, Mapping) else {}
    impersonate = str(fp.get("impersonate") or "chrome136").strip()
    try:
        from curl_cffi.requests import Session as CurlSession

        try:
            session = CurlSession(impersonate=impersonate)
        except Exception:
            session = CurlSession(impersonate="chrome136")
    except ImportError:
        import requests

        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    flow_id = request.flow_id
    # oai-session-id 与设备绑定：指纹档案提供则全程不变（含跨任务）
    session_id = str(fp.get("oai_session_id") or "").strip() or session_ids.get(flow_id)
    if not session_id:
        session_id = str(uuid.uuid4())
        session_ids[flow_id] = session_id
    sec_ch_ua = str(fp.get("sec_ch_ua") or "").strip()
    headers = {
        "User-Agent": str(fp.get("ua") or _CHATGPT_USER_AGENT),
        "Accept": "*/*",
        "Accept-Language": str(fp.get("accept_language") or _request_accept_language(request)),
        "Authorization": f"Bearer {request.access_token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Content-Type": "application/json",
        "oai-device-id": _request_device_id(request),
        "oai-language": str(fp.get("oai_language") or str(request.contract.payment_locale or "id").strip()),
        "oai-session-id": session_id,
        "oai-client-version": _CHATGPT_CLIENT_VERSION,
        "oai-client-build-number": _CHATGPT_CLIENT_BUILD,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if sec_ch_ua:
        headers["sec-ch-ua"] = sec_ch_ua
        headers["sec-ch-ua-mobile"] = str(fp.get("sec_ch_ua_mobile") or "?0")
        headers["sec-ch-ua-platform"] = str(fp.get("sec_ch_ua_platform") or '"Windows"')
    account_id = str(
        request.auth_context.get("chatgpt_account_id")
        or request.auth_context.get("account_id")
        or ""
    ).strip()
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    cookie_header = _chatgpt_cookie_header(request, _request_device_id(request))
    if cookie_header:
        headers["Cookie"] = cookie_header
    session.headers.update(headers)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    else:
        session.proxies = {}
    return session


def _clone_http_session(source: Any, proxy: str, impersonate: str | None = None) -> Any:
    try:
        from curl_cffi.requests import Session as CurlSession

        try:
            session = CurlSession(impersonate=impersonate or "chrome136")
        except Exception:
            session = CurlSession(impersonate="chrome136")
    except ImportError:
        import requests

        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    try:
        session.headers.update(dict(getattr(source, "headers", {}) or {}))
    except Exception:
        pass
    try:
        session.cookies.update(getattr(source, "cookies", {}) or {})
    except Exception:
        pass
    cookie_header = str(getattr(source, "headers", {}).get("Cookie") or "").strip()
    if cookie_header:
        session.headers["Cookie"] = cookie_header
    session.proxies = {"http": proxy, "https": proxy} if proxy else {}
    return session


def _sync_session_cookies(target: Any, source: Any) -> None:
    if target is source:
        return
    try:
        target.cookies.update(source.cookies)
    except Exception:
        pass
    try:
        source_cookie = str(getattr(source, "headers", {}).get("Cookie") or "").strip()
        if source_cookie:
            target.headers["Cookie"] = _merge_cookie_headers(
                str(getattr(target, "headers", {}).get("Cookie") or ""),
                source_cookie,
            )
        jar = getattr(source, "cookies", None)
        if jar is not None:
            jar_cookie = "; ".join(
                f"{name}={value}"
                for name, value in jar.items()
                if name and value is not None
            )
            if jar_cookie:
                target.headers["Cookie"] = _merge_cookie_headers(
                    str(getattr(target, "headers", {}).get("Cookie") or ""),
                    jar_cookie,
                )
    except Exception:
        pass


def _safe_close(session: Any) -> None:
    try:
        session.close()
    except Exception:
        pass


def _merge_cookie_headers(*sources: str) -> str:
    values: dict[str, str] = {}
    for source in sources:
        for item in str(source or "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name.strip():
                values[name.strip()] = value.strip()
    return "; ".join(f"{name}={value}" for name, value in values.items())


def _chatgpt_cookie_header(
    request: WalletTransportRequest,
    device_id: str,
    *,
    base_cookie: str = "",
) -> str:
    base = str(base_cookie or request.auth_context.get("cookie_header") or "").strip()
    session_token = str(request.auth_context.get("session_token") or "").strip()
    if not session_token:
        auth_session = request.auth_context.get("auth_session")
        if isinstance(auth_session, Mapping):
            session_token = str(auth_session.get("sessionToken") or "").strip()
    pairs: dict[str, str] = {}
    for source in (base, f"oai-did={device_id}" if device_id else "",):
        for item in source.split(";"):
            name, sep, value = item.strip().partition("=")
            if sep and name:
                pairs[name.strip()] = value.strip()
    if session_token and "__Secure-next-auth.session-token" not in pairs:
        pairs["__Secure-next-auth.session-token"] = session_token
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def _find_intent_reference(payload: Any, depth: int = 0) -> tuple[str, str, str] | None:
    if depth > 12:
        return None
    if isinstance(payload, Mapping):
        object_name = str(payload.get("object") or "").strip().lower()
        intent_id = str(payload.get("id") or "").strip()
        client_secret = str(payload.get("client_secret") or "").strip()
        if (
            intent_id.startswith("pi_")
            and client_secret
            and object_name in {"", "payment_intent"}
        ):
            return intent_id, client_secret, "payment_intents"
        if (
            intent_id.startswith("seti_")
            and client_secret
            and object_name in {"", "setup_intent"}
        ):
            return intent_id, client_secret, "setup_intents"
        for value in payload.values():
            found = _find_intent_reference(value, depth + 1)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found = _find_intent_reference(value, depth + 1)
            if found:
                return found
    return None


def _url_fingerprint(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    return f"{parsed.scheme.lower()}://{str(parsed.hostname or '').lower()}{parsed.path}?{parsed.query}"


def _is_cloudflare_response(response: Any) -> bool:
    headers = getattr(response, "headers", {}) or {}
    server = str(headers.get("server") or headers.get("Server") or "").lower()
    if "cloudflare" not in server and not (headers.get("cf-ray") or headers.get("CF-Ray")):
        return False
    body = str(getattr(response, "text", "") or "")[:2000].lower()
    return bool(headers.get("cf-ray") or headers.get("CF-Ray")) or "cloudflare" in body or "just a moment" in body


def _response_error_summary(response: Any, limit: int = 500) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            parts = [
                str(error.get("type") or "").strip(),
                str(error.get("code") or "").strip(),
                str(error.get("param") or "").strip(),
                str(error.get("message") or "").strip(),
            ]
            text = "; ".join(part for part in parts if part)
            if text:
                return redact_sensitive_text(text)[:limit]
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(getattr(response, "text", "") or "")
    text = re.sub(r"\s+", " ", text).strip()
    return redact_sensitive_text(text)[:limit] or "no response body"


__all__ = ["ChatGPTStripeWalletTransport", "WalletHTTPError"]
