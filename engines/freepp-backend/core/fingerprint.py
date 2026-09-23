"""Per-channel browser fingerprint profiles for the standalone chain engine.

The Node scheduler has its own fingerprint factory.  The FastAPI chain runs
independently, so it needs a small local equivalent to keep every request in a
single attempt on the same UA/TLS/device identity while allowing retries to
select a different profile.
"""
from __future__ import annotations

import random
import uuid
from typing import Any

from .config import settings


_LOCALES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "AE": (("en-AE", "ar-AE"), ("Asia/Dubai",)),
    "AU": (("en-AU",), ("Australia/Sydney",)),
    "BR": (("pt-BR",), ("America/Sao_Paulo", "America/Fortaleza")),
    "CA": (("en-CA", "fr-CA"), ("America/Toronto", "America/Vancouver")),
    "DE": (("de-DE",), ("Europe/Berlin",)),
    "ES": (("es-ES",), ("Europe/Madrid",)),
    "FR": (("fr-FR",), ("Europe/Paris",)),
    "GB": (("en-GB",), ("Europe/London",)),
    "HK": (("zh-HK", "en-HK"), ("Asia/Hong_Kong",)),
    "ID": (("id-ID", "en-ID"), ("Asia/Jakarta",)),
    "IN": (("en-IN", "hi-IN"), ("Asia/Kolkata",)),
    "IT": (("it-IT",), ("Europe/Rome",)),
    "JP": (("ja-JP",), ("Asia/Tokyo",)),
    "KR": (("ko-KR",), ("Asia/Seoul",)),
    "MY": (("ms-MY", "en-MY"), ("Asia/Kuala_Lumpur",)),
    "NL": (("nl-NL",), ("Europe/Amsterdam",)),
    "PH": (("en-PH", "fil-PH"), ("Asia/Manila",)),
    "PL": (("pl-PL",), ("Europe/Warsaw",)),
    "PT": (("pt-PT",), ("Europe/Lisbon",)),
    "SA": (("ar-SA", "en-SA"), ("Asia/Riyadh",)),
    "SG": (("en-SG",), ("Asia/Singapore",)),
    "TH": (("th-TH", "en-TH"), ("Asia/Bangkok",)),
    "TR": (("tr-TR",), ("Europe/Istanbul",)),
    "TW": (("zh-TW",), ("Asia/Taipei",)),
    "US": (("en-US",), ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles")),
    "VN": (("vi-VN",), ("Asia/Ho_Chi_Minh", "Asia/Bangkok")),
    "ZA": (("en-ZA", "af-ZA"), ("Africa/Johannesburg",)),
}

_CHROME = ("chrome136", "chrome131", "chrome124", "chrome116")
_EDGE = ("edge101", "edge99")
_FIREFOX = ("firefox133", "firefox128")

# Branch names not exposed by the Node UI still use the same channel policy in
# the standalone FastAPI engine.  Aliases keep qris/direct/grok consistent
# with the payment method that actually reaches Stripe.
_CHANNEL_ALIASES = {
    "qris": "gopay",
    "direct": "card",
    "grok": "card",
    "grabpay": "gcash",
}
_CHANNEL_POOLS: dict[str, tuple[str, ...]] = {
    "gcash": _CHROME,
    "upi": _CHROME,
    "paypal": _CHROME + _EDGE + _FIREFOX,
    "gopay": _CHROME + _EDGE + _FIREFOX,
    "momo": _CHROME + _EDGE + _FIREFOX,
    "card": _CHROME + _EDGE + _FIREFOX,
    "pix": _CHROME + _EDGE + _FIREFOX,
}


def _locale(country: str) -> tuple[str, str]:
    locales, timezones = _LOCALES.get(str(country or "").upper(), _LOCALES["US"])
    return random.choice(locales), random.choice(timezones)


def _available_targets() -> set[str]:
    """Return curl_cffi targets when the installed version exposes the enum."""
    try:
        from curl_cffi.requests.impersonate import BrowserType  # type: ignore

        return {str(item.value) for item in BrowserType}
    except Exception:
        return set()


def _target_for(channel: str, kernel: str | None = None) -> tuple[str, str]:
    canonical = _CHANNEL_ALIASES.get(channel, channel)
    pool = _CHANNEL_POOLS.get(canonical, _CHANNEL_POOLS["paypal"])
    available = _available_targets()
    candidates = [target for target in pool if not available or target in available]
    if kernel:
        requested = str(kernel).lower()
        candidates = [target for target in candidates if target.startswith(requested)]
    if not candidates:
        configured = str(settings.tls.get("impersonate", "chrome136") or "chrome136")
        for prefix in ("chrome", "edge", "firefox", "safari"):
            if configured.startswith(prefix):
                return configured, prefix
        return configured, "chrome"
    target = random.choice(candidates)
    if target.startswith("chrome"):
        return target, "chrome"
    if target.startswith("edge"):
        return target, "edge"
    if target.startswith("firefox"):
        return target, "firefox"
    return target, target.split(".", 1)[0]


def _user_agent(target: str, kernel: str, platform: str) -> str:
    if kernel == "firefox":
        version = target.removeprefix("firefox") or "128"
        if platform == "macOS":
            return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{version}.0) Gecko/20100101 Firefox/{version}.0"
        return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}.0) Gecko/20100101 Firefox/{version}.0"
    if kernel == "edge":
        version = target.removeprefix("edge") or "99"
        edge_version = "101.0.1210.47" if version == "101" else "99.0.1150.46"
        base = "Macintosh; Intel Mac OS X 10_15_7" if platform == "macOS" else "Windows NT 10.0; Win64; x64"
        return f"Mozilla/5.0 ({base}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36 Edg/{edge_version}"
    version = target.removeprefix("chrome") or "136"
    base = "Macintosh; Intel Mac OS X 10_15_7" if platform == "macOS" else "Windows NT 10.0; Win64; x64"
    return f"Mozilla/5.0 ({base}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"


def generate(channel: str = "paypal", country: str = "US", *, kernel: str | None = None) -> dict[str, Any]:
    """Generate one complete profile for a single chain attempt."""
    channel = str(channel or "paypal").strip().lower()
    canonical = _CHANNEL_ALIASES.get(channel, channel)
    target, inferred_kernel = _target_for(canonical, kernel)
    # Wallet/browser fallback paths are Chromium-only; other channels can
    # rotate the complete browser family.
    if canonical in {"gcash", "upi"}:
        target, inferred_kernel = _target_for(canonical, "chrome")
    kernel_name = inferred_kernel
    platform = "macOS" if kernel_name == "safari" else random.choice(("Windows", "Windows", "Windows", "macOS"))
    locale, timezone = _locale(country)
    language = locale.split("-", 1)[0]
    ua = _user_agent(target, kernel_name, platform)
    sec_ch_ua = ""
    if kernel_name == "chrome":
        version = target.removeprefix("chrome") or "136"
        sec_ch_ua = f'"Google Chrome";v="{version}", "Not.A/Brand";v="8", "Chromium";v="{version}"'
    elif kernel_name == "edge":
        version = target.removeprefix("edge") or "99"
        sec_ch_ua = f'"Microsoft Edge";v="{version}", "Not.A/Brand";v="8", "Chromium";v="{version}"'
    session_id = str(uuid.uuid4())
    return {
        "channel": channel,
        "country": str(country or "US").upper(),
        "kernel": kernel_name,
        "kernel_label": kernel_name.capitalize(),
        "impersonate": target,
        "ua": ua,
        "accept_language": f"{locale},{language};q=0.9,en;q=0.8",
        "oai_language": locale,
        "locale": locale,
        "timezone": timezone,
        "platform": "MacIntel" if platform == "macOS" else "Win32",
        "platform_os": platform,
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": f'"{platform}"',
        "device_id": str(uuid.uuid4()),
        "session_seed": session_id,
        "oai_session_id": session_id,
    }


def summary(fingerprint: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(fingerprint, dict):
        return None
    return {
        "channel": str(fingerprint.get("channel") or ""),
        "country": str(fingerprint.get("country") or ""),
        "kernel": str(fingerprint.get("kernel") or ""),
        "kernel_label": str(fingerprint.get("kernel_label") or ""),
        "impersonate": str(fingerprint.get("impersonate") or ""),
        "device_id": str(fingerprint.get("device_id") or "")[:8],
        "locale": str(fingerprint.get("locale") or ""),
        "timezone": str(fingerprint.get("timezone") or ""),
    }


__all__ = ["generate", "summary"]
