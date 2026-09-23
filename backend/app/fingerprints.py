from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FILE = ROOT / "data" / "fingerprints.json"

COUNTRY_LOCALES: dict[str, dict[str, tuple[str, ...]]] = {
    "AE": {"locales": ("en-AE", "ar-AE"), "tz": ("Asia/Dubai",)},
    "AR": {"locales": ("es-AR",), "tz": ("America/Argentina/Buenos_Aires",)},
    "AU": {"locales": ("en-AU",), "tz": ("Australia/Sydney", "Australia/Melbourne")},
    "BR": {"locales": ("pt-BR",), "tz": ("America/Sao_Paulo", "America/Fortaleza")},
    "CA": {"locales": ("en-CA", "fr-CA"), "tz": ("America/Toronto", "America/Vancouver")},
    "CL": {"locales": ("es-CL",), "tz": ("America/Santiago",)},
    "CO": {"locales": ("es-CO",), "tz": ("America/Bogota",)},
    "DE": {"locales": ("de-DE",), "tz": ("Europe/Berlin", "Europe/Frankfurt")},
    "ES": {"locales": ("es-ES",), "tz": ("Europe/Madrid", "Europe/Barcelona")},
    "FR": {"locales": ("fr-FR",), "tz": ("Europe/Paris",)},
    "GB": {"locales": ("en-GB",), "tz": ("Europe/London",)},
    "HK": {"locales": ("zh-HK", "en-HK"), "tz": ("Asia/Hong_Kong",)},
    "ID": {"locales": ("id-ID", "en-ID"), "tz": ("Asia/Jakarta",)},
    "IN": {"locales": ("en-IN", "hi-IN"), "tz": ("Asia/Kolkata",)},
    "IT": {"locales": ("it-IT",), "tz": ("Europe/Rome",)},
    "JP": {"locales": ("ja-JP",), "tz": ("Asia/Tokyo",)},
    "KE": {"locales": ("en-KE", "sw-KE"), "tz": ("Africa/Nairobi",)},
    "KR": {"locales": ("ko-KR",), "tz": ("Asia/Seoul",)},
    "MX": {"locales": ("es-MX",), "tz": ("America/Mexico_City",)},
    "MY": {"locales": ("ms-MY", "en-MY"), "tz": ("Asia/Kuala_Lumpur",)},
    "NG": {"locales": ("en-NG",), "tz": ("Africa/Lagos",)},
    "NL": {"locales": ("nl-NL",), "tz": ("Europe/Amsterdam",)},
    "NZ": {"locales": ("en-NZ",), "tz": ("Pacific/Auckland",)},
    "PE": {"locales": ("es-PE",), "tz": ("America/Lima",)},
    "PH": {"locales": ("en-PH", "fil-PH"), "tz": ("Asia/Manila",)},
    "PL": {"locales": ("pl-PL",), "tz": ("Europe/Warsaw",)},
    "PT": {"locales": ("pt-PT",), "tz": ("Europe/Lisbon",)},
    "SA": {"locales": ("ar-SA", "en-SA"), "tz": ("Asia/Riyadh",)},
    "SE": {"locales": ("sv-SE",), "tz": ("Europe/Stockholm",)},
    "SG": {"locales": ("en-SG",), "tz": ("Asia/Singapore",)},
    "TH": {"locales": ("th-TH", "en-TH"), "tz": ("Asia/Bangkok",)},
    "TR": {"locales": ("tr-TR",), "tz": ("Europe/Istanbul",)},
    "TW": {"locales": ("zh-TW",), "tz": ("Asia/Taipei",)},
    "US": {
        "locales": ("en-US", "en-US", "en-US"),
        "tz": ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Phoenix"),
    },
    "VN": {"locales": ("vi-VN",), "tz": ("Asia/Ho_Chi_Minh", "Asia/Bangkok")},
    "ZA": {"locales": ("en-ZA", "af-ZA"), "tz": ("Africa/Johannesburg",)},
}

CHROME_VARIANTS: dict[str, dict[str, str]] = {
    "chrome152": {"version": "152", "sec": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"'},
    "chrome150": {"version": "150", "sec": '"Chromium";v="150", "Not?A_Brand";v="24", "Google Chrome";v="150"'},
    "chrome146": {"version": "146", "sec": '"Chromium";v="146", "Not?A_Brand";v="24", "Google Chrome";v="146"'},
    "chrome142": {"version": "142", "sec": '"Chromium";v="142", "Not?A_Brand";v="24", "Google Chrome";v="142"'},
    "chrome136": {"version": "136", "sec": '"Google Chrome";v="136", "Not.A/Brand";v="8", "Chromium";v="136"'},
    "chrome131": {"version": "131", "sec": '"Google Chrome";v="131", "Not.A/Brand";v="24", "Chromium";v="131"'},
    "chrome124": {"version": "124", "sec": '"Google Chrome";v="124", "Not.A/Brand";v="8", "Chromium";v="124"'},
    "chrome116": {"version": "116", "sec": '"Google Chrome";v="116", "Not.A/Brand";v="24", "Chromium";v="116"'},
}

EDGE_VARIANTS: dict[str, dict[str, str]] = {
    "edge101": {
        "version": "101",
        "edge_version": "101.0.1210.47",
        "sec": '"Microsoft Edge";v="101", "Not.A/Brand";v="99", "Chromium";v="101"',
    },
    "edge99": {
        "version": "99",
        "edge_version": "99.0.1150.46",
        "sec": '"Microsoft Edge";v="99", "Not.A/Brand";v="8", "Chromium";v="99"',
    },
}

SAFARI_VARIANTS = ("safari17_0", "safari15_5")
FIREFOX_VARIANTS = ("firefox147", "firefox144", "firefox135", "firefox133", "firefox128")
CHROME_TARGETS = tuple(CHROME_VARIANTS)
EDGE_TARGETS = tuple(EDGE_VARIANTS)

SCREENS = ("1920x1080", "1920x1080", "2560x1440", "1536x864", "1440x900", "1366x768")
CONCURRENCIES = (4, 8, 8, 12, 16)
MEMORIES = (4, 8, 8, 16)
WEBGL_VENDORS = ("Google Inc. (Intel)", "Google Inc. (NVIDIA)", "Google Inc. (AMD)", "WebKit")
GPU_RENDERERS = (
    "ANGLE (Intel, Intel(R) UHD Graphics 620, OpenGL 4.1)",
    "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060, OpenGL 4.5)",
    "ANGLE (AMD, AMD Radeon(TM) Graphics, OpenGL 4.5)",
    "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, OpenGL 4.6)",
    "Apple GPU",
)
COMMON_FONTS = ("Arial", "Helvetica Neue", "Segoe UI", "Times New Roman", "Courier New", "Verdana", "Tahoma", "Georgia", "Trebuchet MS")

_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def fingerprint_file() -> Path:
    configured = str(os.environ.get("TILIAN_FINGERPRINTS_FILE") or "").strip()
    return Path(configured) if configured else DEFAULT_FILE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    _cache = {"version": 1, "entries": {}}
    path = fingerprint_file()
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                _cache = raw
    except (OSError, ValueError, TypeError):
        _cache = {"version": 1, "entries": {}}
    return _cache


def _save() -> None:
    current = _load()
    path = fingerprint_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(current, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        return


def account_key(email: str, at: str = "") -> str:
    mail = str(email or "").strip().lower()
    if mail:
        return mail
    value = str(at or "").strip()
    if value:
        return "at-" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return "anon"


def store_key(country: str, key: str, channel: str) -> str:
    prefix = f"{str(channel or '').strip().lower()}|" if channel else ""
    return f"{prefix}{str(country or '').upper()}|{key}"


def _country_profile(country: str) -> dict[str, tuple[str, ...]]:
    return COUNTRY_LOCALES.get(str(country or "").upper(), {"locales": ("en-US",), "tz": ("America/New_York",)})


def _chrome_ua(version: str, platform: str) -> str:
    if platform == "macOS":
        return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"


def _edge_ua(version: str, edge_version: str, platform: str) -> str:
    if platform == "macOS":
        return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36 Edg/{edge_version}"
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36 Edg/{edge_version}"


def _safari_ua(version: str) -> str:
    return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{version} Safari/605.1.15"


def _firefox_ua(version: str, platform: str) -> str:
    if platform == "macOS":
        return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{version}.0) Gecko/20100101 Firefox/{version}.0"
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}.0) Gecko/20100101 Firefox/{version}.0"


def _parse_target(target: str) -> str:
    digits = "".join(character for character in target if character.isdigit())
    if target.startswith("safari") and len(digits) >= 2:
        return f"{digits[:-1]}.{digits[-1]}"
    return digits


# UPI 的 upi-git5 通道在源站固定以 macOS 身份出现（"纯协议 · 无浏览器内核"）。
# 这四个档案与源站 UPI_GIT5_MACOS_PROFILES 同值；后缀 -macos 只表示平台，
# 传给 curl_cffi 时必须剥掉。
UPI_GIT5_MACOS_PROFILES = (
    "chrome146-macos",
    "chrome150-macos",
    "firefox147-macos",
    "chrome136-macos",
)
MACOS_SUFFIX = "-macos"


def _strip_macos(target: str) -> tuple[str, bool]:
    """把 `chrome146-macos` 拆成 ("chrome146", True)。"""
    value = str(target or "").strip().lower()
    if value.endswith(MACOS_SUFFIX):
        return value[: -len(MACOS_SUFFIX)], True
    return value, False


def generate(
    country: str,
    *,
    channel: str = "",
    kernel: str = "",
    impersonate: str = "",
    platform: str = "",
    locale: str = "",
) -> dict[str, Any]:
    country_code = str(country or "US").upper()
    profile = _country_profile(country_code)
    # 调用方可以钉死语言。UPI 的 git5 rail 固定 en-IN，而 IN 的随机池里还有
    # hi-IN——让指纹留在 hi-IN 会和该 rail 的其余身份字段自相矛盾。
    locale = str(locale or "").strip() or secrets.choice(profile["locales"])
    language = locale.split("-")[0]
    timezone = secrets.choice(profile["tz"])
    kernels = ("chrome", "edge", "safari", "firefox")
    selected_kernel = str(kernel or secrets.choice(kernels)).lower()
    if selected_kernel not in kernels:
        selected_kernel = "chrome"
    # `impersonate` 参数以前在这里被无条件清空，导致调用方（worker 的 UPI 轮换）
    # 指定的 TLS 目标被静默丢弃、退化成 secrets.choice。必须先解析出请求目标，
    # 再由它推导平台。
    requested_impersonate, wants_macos = _strip_macos(impersonate)
    if requested_impersonate not in CHROME_TARGETS and selected_kernel == "chrome":
        # 目标可能属于别的内核族（例如把 chrome146 传给了 firefox 内核）；
        # 只在同族内回退，避免 UA 与 TLS 档案跨族不一致。
        requested_impersonate = ""
    if requested_impersonate not in EDGE_TARGETS and selected_kernel == "edge":
        requested_impersonate = ""
    if requested_impersonate not in FIREFOX_VARIANTS and selected_kernel == "firefox":
        requested_impersonate = ""
    if requested_impersonate not in SAFARI_VARIANTS and selected_kernel == "safari":
        requested_impersonate = ""
    requested_platform = str(platform or "").strip()
    if requested_platform:
        macos = requested_platform.lower() == "macos"
    elif wants_macos:
        macos = True
    else:
        macos = selected_kernel == "safari"
    platform = "macOS" if macos else "Windows"
    platform_label = "MacIntel" if macos else "Win32"
    sec_ch_ua = ""

    if selected_kernel == "chrome":
        impersonate = requested_impersonate or secrets.choice(CHROME_TARGETS)
        variant = CHROME_VARIANTS[impersonate]
        user_agent = _chrome_ua(variant["version"], platform)
        sec_ch_ua = variant["sec"]
    elif selected_kernel == "edge":
        impersonate = requested_impersonate or secrets.choice(EDGE_TARGETS)
        variant = EDGE_VARIANTS[impersonate]
        user_agent = _edge_ua(variant["version"], variant["edge_version"], platform)
        sec_ch_ua = variant["sec"]
    elif selected_kernel == "safari":
        impersonate = requested_impersonate or secrets.choice(SAFARI_VARIANTS)
        user_agent = _safari_ua(_parse_target(impersonate))
    else:
        impersonate = requested_impersonate or secrets.choice(FIREFOX_VARIANTS)
        user_agent = _firefox_ua(_parse_target(impersonate), platform)

    session_seed = str(uuid.uuid4())
    created = _now_iso()
    return {
        "country": country_code,
        "channel": channel,
        "kernel": selected_kernel,
        "kernel_label": selected_kernel.capitalize(),
        "impersonate": impersonate,
        "ua": user_agent,
        "accept_language": f"{locale},{language};q=0.9,en;q=0.8",
        "oai_language": locale,
        "locale": locale,
        "timezone": timezone,
        "platform": platform_label,
        "platform_os": platform,
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": f'"{platform}"',
        "device_id": str(uuid.uuid4()),
        "session_seed": session_seed,
        "oai_session_id": session_seed,
        "screen": secrets.choice(SCREENS),
        "color_depth": 24,
        "hardware_concurrency": secrets.choice(CONCURRENCIES),
        "device_memory": secrets.choice(MEMORIES),
        "max_touch_points": 0,
        "canvas_seed": secrets.token_hex(4),
        "webgl_vendor": secrets.choice(WEBGL_VENDORS),
        "gpu_renderer": secrets.choice(GPU_RENDERERS),
        "fonts": list(COMMON_FONTS[: secrets.randbelow(4) + 5]),
        "created_at": created,
        "updated_at": created,
        "use_count": 0,
    }


def get_or_create(*, country: str, email: str = "", at: str = "", channel: str = "") -> dict[str, Any]:
    country_code = str(country or "US").upper()
    channel_id = str(channel or "").strip().lower()
    with _lock:
        database = _load()
        entries = database.setdefault("entries", {})
        key = store_key(country_code, account_key(email, at), channel_id)
        current = entries.get(key)
        if not isinstance(current, dict):
            current = generate(country_code, channel=channel_id)
            if email:
                current["account_email"] = email.strip().lower()
            entries[key] = current
            _save()
            return dict(current)
        current["updated_at"] = _now_iso()
        current["use_count"] = int(current.get("use_count") or 0) + 1
        _save()
        return dict(current)


def hint(country: str, email: str = "", at: str = "", channel: str = "") -> dict[str, Any]:
    country_code = str(country or "US").upper()
    channel_id = str(channel or "").strip().lower()
    with _lock:
        entries = _load().setdefault("entries", {})
        current = entries.get(store_key(country_code, account_key(email, at), channel_id))
        if not isinstance(current, dict):
            return {"country": country_code, "channel": channel_id}
        return {
            "country": current.get("country") or country_code,
            "channel": current.get("channel") or channel_id,
            "kernel": current.get("kernel") or "",
            "kernel_label": current.get("kernel_label") or "",
            "impersonate": current.get("impersonate") or "",
            "device_id": str(current.get("device_id") or "")[:8],
            "timezone": current.get("timezone") or "",
            "locale": current.get("locale") or "",
        }
