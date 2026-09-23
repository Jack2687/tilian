"""Browser-only GoPay checkout handoff to the QR authorization page."""

from __future__ import annotations

import html
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .paypal_proxy import normalize_proxy_url
from .wallet_provider import random_billing_details, redact_sensitive_text


GOPAY_AUTHORIZE_URL_RE = re.compile(
    r"https?://(?:"
    r"pm-redirects\.stripe\.com/authorize/acct_[A-Za-z0-9]+/(?:sa|pa)_nonce_[A-Za-z0-9]+"
    r"|app\.midtrans\.com/(?:snap/v[34]/redirection/[0-9a-f-]{36}|pm-redirects/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+)"
    r")",
    re.IGNORECASE,
)

_GOPAY_SELECTORS = (
    'input#payment-method-accordion-item-title-gopay',
    'input[value="gopay"][name="payment-method-accordion-item-title"]',
    '#payment-method-label-gopay',
    '[role="radio"][aria-label*="gopay" i]',
    'button[aria-label*="gopay" i]',
    'button:has-text("GoPay")',
    'label:has-text("GoPay")',
    '[data-testid="gopay-accordion-item-button"]',
    '[data-testid*="gopay" i]',
    '[id*="gopay" i]',
)

_SUBMIT_SELECTORS = (
    'button[data-testid="hosted-payment-submit-button"]',
    '[data-testid*="submit" i]',
    'button:has-text("Subscribe")',
    'button:has-text("Start trial")',
    'button:has-text("Pay")',
    'button[type="submit"]',
    'input[type="submit"]',
)


def extract_gopay_midtrans_url(
    checkout_url: str,
    *,
    proxy: str = "",
    cookie_header: str = "",
    timeout_seconds: int = 300,
    headless: bool = False,
    browser_engine: str = "auto",
    cdp_url: str = "",
    billing_details: Mapping[str, Any] | None = None,
) -> str:
    """Open a ChatGPT checkout, select GoPay, and capture the QR authorization URL."""
    target = str(checkout_url or "").strip()
    if not _is_supported_checkout_url(target):
        raise RuntimeError("GoPay browser extractor requires a ChatGPT or Stripe hosted checkout URL")

    timeout = max(30, int(timeout_seconds or 300))
    engine = str(browser_engine or "auto").strip().lower()
    cdp_target = str(cdp_url or "").strip()
    attempts = ["cdp", "camoufox", "playwright"] if engine in {"", "auto"} and cdp_target else (
        ["camoufox", "playwright"] if engine in {"", "auto"} else [engine]
    )
    found_url, errors = _extract_gopay_with_attempts(
        target,
        attempts=attempts,
        cdp_target=cdp_target,
        proxy=proxy,
        cookie_header=cookie_header,
        timeout=timeout,
        headless=headless,
        billing_details=billing_details,
    )
    if found_url:
        return found_url
    detail = ", ".join(errors) or "no browser engine available"
    raise RuntimeError(f"GoPay checkout did not reach the GoPay QR authorization page ({detail})")


def _extract_gopay_with_attempts(
    target: str,
    *,
    attempts: list[str],
    cdp_target: str,
    proxy: str,
    cookie_header: str,
    timeout: int,
    headless: bool,
    billing_details: Mapping[str, Any] | None,
) -> tuple[str, list[str]]:
    errors: list[str] = []
    for candidate in attempts:
        try:
            if candidate in {"cdp", "roxy", "roxy_cdp", "existing", "existing_browser"}:
                return (
                    _extract_with_cdp(
                        target,
                        cdp_url=cdp_target,
                        timeout_seconds=timeout,
                        billing_details=billing_details,
                    ),
                    [],
                )
            if candidate == "camoufox":
                return (
                    _extract_with_camoufox(
                        target,
                        proxy=proxy,
                        cookie_header=cookie_header,
                        timeout_seconds=timeout,
                        headless=headless,
                        billing_details=billing_details,
                    ),
                    [],
                )
            if candidate == "playwright":
                return (
                    _extract_with_playwright(
                        target,
                        proxy=proxy,
                        cookie_header=cookie_header,
                        timeout_seconds=timeout,
                        headless=headless,
                        billing_details=billing_details,
                    ),
                    [],
                )
            raise RuntimeError(f"unsupported GoPay browser engine: {candidate}")
        except Exception as exc:
            errors.append(f"{candidate}:{type(exc).__name__}:{redact_sensitive_text(exc)}")
    return "", errors


def _extract_with_camoufox(
    checkout_url: str,
    *,
    proxy: str,
    cookie_header: str,
    timeout_seconds: int,
    headless: bool,
    billing_details: Mapping[str, Any] | None,
) -> str:
    from camoufox.sync_api import Camoufox

    proxy_config = _camoufox_proxy(proxy)
    profile_dir = tempfile.mkdtemp(prefix="gopay_camoufox_")
    options: dict[str, Any] = {
        "headless": bool(headless),
        "humanize": True,
        "block_webrtc": True,
        "geoip": bool(proxy_config),
        "locale": ["id-ID", "en-US", "en"],
        "persistent_context": True,
        "user_data_dir": profile_dir,
    }
    if proxy_config:
        options["proxy"] = proxy_config

    try:
        with Camoufox(**options) as context:
            _add_cookie_header(context, cookie_header)
            page = context.pages[0] if context.pages else context.new_page()
            return _run_checkout_page(page, context, checkout_url, timeout_seconds, billing_details)
    finally:
        _remove_profile(profile_dir)


def _extract_with_playwright(
    checkout_url: str,
    *,
    proxy: str,
    cookie_header: str,
    timeout_seconds: int,
    headless: bool,
    billing_details: Mapping[str, Any] | None,
) -> str:
    from playwright.sync_api import sync_playwright

    browser_proxy = _playwright_proxy(proxy)
    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {"headless": bool(headless)}
        if browser_proxy:
            launch_options["proxy"] = browser_proxy
        browser = _launch_playwright_browser(playwright, launch_options)
        try:
            context = browser.new_context(
                locale="id-ID",
                timezone_id="Asia/Jakarta",
                viewport={"width": 1280, "height": 900},
                extra_http_headers={"Accept-Language": "id-ID,id;q=0.9,en;q=0.8"},
            )
            _add_cookie_header(context, cookie_header)
            page = context.new_page()
            return _run_checkout_page(page, context, checkout_url, timeout_seconds, billing_details)
        finally:
            browser.close()


def _extract_with_cdp(
    checkout_url: str,
    *,
    cdp_url: str,
    timeout_seconds: int,
    billing_details: Mapping[str, Any] | None,
) -> str:
    if not cdp_url:
        raise RuntimeError("CDP browser extraction requires gopay_browser_cdp_url")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=timeout_seconds * 1000)
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            raise RuntimeError("CDP browser did not expose an existing context")
        pages = list(getattr(context, "pages", []) or [])
        if not pages:
            raise RuntimeError("CDP browser did not expose an existing page")
        page = pages[0]
        return _run_checkout_page(page, context, checkout_url, timeout_seconds, billing_details)


def _launch_playwright_browser(playwright: Any, launch_options: dict[str, Any]) -> Any:
    errors: list[str] = []
    candidates: list[dict[str, Any]] = []
    try:
        executable = str(playwright.chromium.executable_path or "").strip()
        if executable:
            candidates.append({"executable_path": executable})
    except Exception:
        pass
    candidates.extend((
        {"channel": "msedge"},
        {"channel": "chrome"},
        {},
    ))
    for extra in candidates:
        try:
            return playwright.chromium.launch(**launch_options, **extra)
        except Exception as exc:
            label = extra.get("channel") or extra.get("executable_path") or "bundled"
            errors.append(f"{label}:{type(exc).__name__}:{exc}")
    raise RuntimeError("; ".join(errors))


def _run_checkout_page(
    page: Any,
    context: Any,
    checkout_url: str,
    timeout_seconds: int,
    billing_details: Mapping[str, Any] | None = None,
) -> str:
    captured_authorize_urls: list[str] = []

    def remember_url(value: Any) -> None:
        url = _extract_gopay_authorize_url(value)
        if url:
            captured_authorize_urls.append(url)

    _install_context_page_capture(context, remember_url)
    _install_authorize_abort(context, remember_url)
    for candidate in list(getattr(context, "pages", []) or []) + [page]:
        _install_url_capture(candidate, remember_url)

    page.goto(checkout_url, wait_until="domcontentloaded", timeout=min(timeout_seconds, 120) * 1000)
    _capture_page_text(page, remember_url)
    _capture_frame_text(page, remember_url)
    _wait_for_checkout_controls(page, timeout_seconds=min(timeout_seconds, 90))

    if not _click_first_ready(page, _GOPAY_SELECTORS):
        raise RuntimeError("GoPay payment method was not rendered")
    billing = dict(billing_details or random_billing_details("ID"))
    _fill_billing_details_if_present(page, billing)
    _click_terms_if_present(page)
    if not _click_first_ready(page, _SUBMIT_SELECTORS):
        _click_text_button(page)
    _capture_page_text(page, remember_url)
    _capture_frame_text(page, remember_url)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if captured_authorize_urls:
            return captured_authorize_urls[0]
        _capture_page_text(page, remember_url)
        _capture_frame_text(page, remember_url)
        if captured_authorize_urls:
            return captured_authorize_urls[0]
        for candidate in list(getattr(context, "pages", []) or []) + [page]:
            _install_url_capture(candidate, remember_url)
            url = str(getattr(candidate, "url", "") or "").strip()
            found = _extract_gopay_authorize_url(url)
            if found:
                return found
            for frame in _iter_frames(candidate):
                frame_url = str(getattr(frame, "url", "") or "").strip()
                found = _extract_gopay_authorize_url(frame_url)
                if found:
                    return found
        try:
            page.wait_for_timeout(750)
        except Exception:
            time.sleep(0.75)
    urls = []
    for candidate in list(getattr(context, "pages", []) or []) + [page]:
        try:
            current = str(getattr(candidate, "url", "") or "").strip()
            if current and current not in urls:
                urls.append(current)
        except Exception:
            pass
    raise RuntimeError(
        "GoPay QR authorization redirect timed out; "
        f"pages={urls[:5]}; {_checkout_debug_snapshot(page)}"
    )


def _install_context_page_capture(context: Any, callback: Any) -> None:
    if getattr(context, "_gopay_page_capture_installed", False):
        return
    try:
        context.on("page", lambda page: _install_url_capture(page, callback))
        setattr(context, "_gopay_page_capture_installed", True)
    except Exception:
        pass


def _install_authorize_abort(context: Any, callback: Any) -> None:
    if getattr(context, "_gopay_authorize_abort_installed", False):
        return
    try:
        def _route_handler(route: Any) -> None:
            request = getattr(route, "request", None)
            url = str(getattr(request, "url", "") or "").strip()
            found = _extract_gopay_authorize_url(url)
            if found:
                callback(found)
                route.abort()
                return
            route.continue_()

        context.route("**/*", _route_handler)
        setattr(context, "_gopay_authorize_abort_installed", True)
    except Exception:
        pass


def _install_url_capture(page: Any, callback: Any) -> None:
    if getattr(page, "_gopay_url_capture_installed", False):
        return
    try:
        page.on("request", lambda request: callback(getattr(request, "url", "")))
        def _on_response(response: Any) -> None:
            callback(getattr(response, "url", ""))
            try:
                headers = getattr(response, "headers", {}) or {}
                content_type = str(
                    headers.get("content-type")
                    or headers.get("Content-Type")
                    or ""
                ).lower()
                if content_type and not any(marker in content_type for marker in ("json", "html", "text", "javascript")):
                    return
                body = str(response.text() or "")
                if body:
                    callback(body)
            except Exception:
                pass
        page.on("response", _on_response)
        page.on("framenavigated", lambda frame: callback(getattr(frame, "url", "")))
        setattr(page, "_gopay_url_capture_installed", True)
    except Exception:
        pass


def _capture_page_text(page: Any, callback: Any) -> None:
    try:
        callback(page.content())
    except Exception:
        pass
    try:
        body = page.locator("body").inner_text(timeout=1500)
        if body:
            callback(body)
    except Exception:
        pass


def _capture_frame_text(page: Any, callback: Any) -> None:
    for frame in _iter_frames(page):
        try:
            callback(frame.content())
        except Exception:
            pass
        try:
            body = frame.locator("body").inner_text(timeout=1000)
            if body:
                callback(body)
        except Exception:
            pass


def _wait_for_checkout_controls(page: Any, *, timeout_seconds: int) -> None:
    deadline = time.time() + max(5, int(timeout_seconds or 90))
    ready_selectors = _GOPAY_SELECTORS + _SUBMIT_SELECTORS
    while time.time() < deadline:
        for selector in ready_selectors:
            try:
                locator = _first_ready_locator(page, selector)
                if _locator_ready(locator):
                    return
            except Exception:
                pass
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)
    current_url = ""
    title = ""
    text = ""
    try:
        current_url = str(getattr(page, "url", "") or "").strip()
    except Exception:
        pass
    try:
        title = str(page.title() or "").strip()
    except Exception:
        pass
    try:
        text = str(page.locator("body").inner_text(timeout=2000) or "").strip()[:500]
    except Exception:
        pass
    raise RuntimeError(
        "ChatGPT checkout page did not render payment controls; "
        f"url={current_url!r}; title={title!r}; body={text!r}; "
        f"{_checkout_debug_snapshot(page)}"
    )


def _checkout_debug_snapshot(page: Any) -> str:
    """Return bounded, credential-free diagnostics for a stuck hosted checkout."""
    counts: dict[str, int] = {}
    for label, selectors in {
        "gopay": _GOPAY_SELECTORS,
        "submit": _SUBMIT_SELECTORS,
    }.items():
        total = 0
        for selector in selectors:
            try:
                total += int(page.locator(selector).count())
            except Exception:
                pass
        counts[label] = total
    try:
        title = str(page.title() or "").strip()[:120]
    except Exception:
        title = ""
    try:
        body = re.sub(r"\s+", " ", str(page.locator("body").inner_text(timeout=1500) or "")).strip()[:800]
    except Exception:
        body = ""
    return f"title={title!r}; controls={counts}; body={body!r}"


def _click_first_ready(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = _first_ready_locator(page, selector)
            if not _locator_ready(locator):
                continue
            tag = str(locator.evaluate("(el) => el.tagName") or "").lower()
            if tag == "input":
                try:
                    locator.check(timeout=3000, force=True)
                except Exception:
                    locator.click(timeout=3000, force=True)
            else:
                locator.click(timeout=3000, force=True)
            page.wait_for_timeout(700)
            return True
        except Exception:
            continue
    return False


def _first_ready_locator(page: Any, selector: str) -> Any:
    scopes = [page, *_iter_frames(page)]
    first_error: Exception | None = None
    for scope in scopes:
        try:
            locator = scope.locator(selector).first
            if _locator_ready(locator):
                return locator
        except Exception as exc:
            first_error = first_error or exc
    if first_error:
        raise first_error
    return page.locator(selector).first


def _iter_frames(page: Any) -> list[Any]:
    try:
        return list(getattr(page, "frames", []) or [])
    except Exception:
        return []


def click_gopay_and_submit(page: Any, context: Any, timeout_seconds: int = 180) -> str:
    """Use the currently loaded checkout page to capture the authorize URL."""
    return _run_checkout_page(
        page,
        context,
        str(getattr(page, "url", "") or ""),
        timeout_seconds,
        random_billing_details("ID"),
    )


def _fill_billing_details_if_present(page: Any, billing: Mapping[str, Any]) -> None:
    if not isinstance(billing, Mapping):
        return
    scopes = [page, *_iter_frames(page)]
    country = str(billing.get("country") or "ID").strip().upper()
    name = str(billing.get("name") or "").strip()
    line1 = str(billing.get("line1") or "").strip()
    city = str(billing.get("city") or "").strip()
    state = str(billing.get("state") or "").strip()
    postal_code = str(billing.get("postal_code") or billing.get("postal") or "").strip()

    _select_country(scopes, country, "Indonesia" if country == "ID" else country)
    _fill_first_text_field(scopes, (
        'input[name="name"]',
        'input[name*="billing"][name*="name" i]',
        'input[id*="billing"][id*="name" i]',
        'input[autocomplete="name"]',
        'input[aria-label*="name" i]',
    ), name)
    _fill_first_text_field(scopes, (
        '#billingAddressLine1',
        'input[name="billingAddressLine1"]',
        'input[name*="addressLine1" i]',
        'input[name*="line1" i]',
        'input[autocomplete="billing address-line1"]',
        'input[autocomplete="address-line1"]',
        'input[aria-label*="address" i]',
    ), line1)
    _fill_first_text_field(scopes, (
        '#billingLocality',
        'input[name="billingLocality"]',
        'input[name*="city" i]',
        'input[name*="locality" i]',
        'input[autocomplete="billing address-level2"]',
        'input[autocomplete="address-level2"]',
        'input[aria-label*="city" i]',
    ), city)
    _fill_first_text_field(scopes, (
        '#billingAdministrativeArea',
        'input[name="billingAdministrativeArea"]',
        'input[name*="state" i]',
        'input[name*="province" i]',
        'input[autocomplete="billing address-level1"]',
        'input[autocomplete="address-level1"]',
        'input[aria-label*="state" i]',
        'input[aria-label*="province" i]',
    ), state)
    _fill_first_text_field(scopes, (
        '#billingPostalCode',
        'input[name="billingPostalCode"]',
        'input[name*="postal" i]',
        'input[name*="zip" i]',
        'input[autocomplete="billing postal-code"]',
        'input[autocomplete="postal-code"]',
        'input[aria-label*="postal" i]',
        'input[aria-label*="zip" i]',
    ), postal_code)
    try:
        page.wait_for_timeout(500)
    except Exception:
        time.sleep(0.5)


def _select_country(scopes: list[Any], country_code: str, country_label: str) -> None:
    selectors = (
        '#billingCountry',
        'select[name="billingCountry"]',
        'select[name*="country" i]',
        'select[autocomplete="billing country"]',
        'select[autocomplete="country"]',
    )
    for scope in scopes:
        for selector in selectors:
            try:
                locator = scope.locator(selector).first
                if not _locator_ready(locator):
                    continue
                try:
                    locator.select_option(value=country_code, timeout=3000)
                    return
                except Exception:
                    locator.select_option(label=country_label, timeout=3000)
                    return
            except Exception:
                continue
    _fill_first_text_field(scopes, (
        'input[name*="country" i]',
        'input[id*="country" i]',
        'input[autocomplete="billing country"]',
        'input[autocomplete="country"]',
    ), country_label)


def _fill_first_text_field(scopes: list[Any], selectors: tuple[str, ...], value: str) -> bool:
    if not value:
        return False
    for scope in scopes:
        for selector in selectors:
            try:
                locator = scope.locator(selector).first
                if not _locator_ready(locator):
                    continue
                try:
                    locator.fill(value, timeout=3000, force=True)
                except Exception:
                    locator.evaluate(
                        """(el, value) => {
                            el.focus();
                            el.value = value;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.blur();
                        }""",
                        value,
                    )
                return True
            except Exception:
                continue
    return False


def _click_terms_if_present(page: Any) -> None:
    selectors = (
        'input[type="checkbox"][name*="terms" i]',
        'input[type="checkbox"][id*="terms" i]',
        'input[type="checkbox"][name*="agree" i]',
        'input[type="checkbox"][id*="agree" i]',
        '[role="checkbox"][aria-label*="terms" i]',
        '[role="checkbox"][aria-label*="agree" i]',
    )
    _click_first_ready(page, selectors)


def _click_text_button(page: Any) -> None:
    import re as _re

    pattern = _re.compile(
        r"subscribe|pay|confirm|continue|submit|订阅|支付|确认|继续",
        _re.IGNORECASE,
    )
    for scope in [page, *_iter_frames(page)]:
        for factory in (
            lambda current=scope: current.get_by_role("button", name=pattern).first,
            lambda current=scope: current.get_by_text(pattern).first,
        ):
            try:
                locator = factory()
                if _locator_ready(locator):
                    locator.click(timeout=5000, force=True)
                    return
            except Exception:
                pass


def _locator_ready(locator: Any) -> bool:
    try:
        return locator.count() > 0 and locator.is_visible() and locator.is_enabled()
    except Exception:
        return False


def _extract_gopay_authorize_url(value: Any) -> str:
    text = _normalize_capture_text(value)
    match = GOPAY_AUTHORIZE_URL_RE.search(text)
    return match.group(0) if match else ""


def _normalize_capture_text(value: Any) -> str:
    text = html.unescape(str(value or "").strip())
    return (
        text.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u002F", "/")
        .replace("&amp;", "&")
    )


def _is_supported_checkout_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return False
    host = str(parsed.netloc or "").lower().rstrip(".")
    path = str(parsed.path or "").rstrip("/")
    return (
        host == "chatgpt.com" and path.startswith("/checkout/")
    ) or (
        host in {"checkout.stripe.com", "pay.openai.com"} and path.startswith("/c/pay/")
    )


def _is_hosted_checkout_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return False
    host = str(parsed.netloc or "").lower().rstrip(".")
    path = str(parsed.path or "").rstrip("/")
    return host in {"checkout.stripe.com", "pay.openai.com"} and path.startswith("/c/pay/")


def _add_cookie_header(context: Any, cookie_header: str) -> None:
    cookies = []
    for item in str(cookie_header or "").split(";"):
        name, separator, value = item.strip().partition("=")
        if not separator or not name:
            continue
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": "chatgpt.com",
            "path": "/",
            "secure": True,
        })
    if cookies:
        context.add_cookies(cookies)


def _playwright_proxy(proxy: str) -> dict[str, str] | None:
    normalized = normalize_proxy_url(proxy)
    if not normalized:
        return None
    parsed = urlsplit(normalized.replace("socks5h://", "socks5://", 1))
    if not parsed.hostname:
        return None
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    result = {"server": server}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


def _camoufox_proxy(proxy: str) -> dict[str, str] | None:
    normalized = normalize_proxy_url(proxy)
    if not normalized:
        return None
    parsed = urlsplit(normalized.replace("socks5h://", "socks5://", 1))
    if not parsed.hostname:
        return None
    result = {
        "server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port}",
        "username": parsed.username or "",
        "password": parsed.password or "",
    }
    return result


def _remove_profile(path: str) -> None:
    try:
        import shutil

        shutil.rmtree(Path(path), ignore_errors=True)
    except Exception:
        pass


__all__ = ["GOPAY_AUTHORIZE_URL_RE", "extract_gopay_midtrans_url"]
