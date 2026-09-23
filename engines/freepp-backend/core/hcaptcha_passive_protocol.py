"""Stripe/hCaptcha passive PoW 协议求解器。

该模块复用 PayPal passive 求解器已经验证过的协议链，但把它整理成一个
与具体钱包无关、可注入测试 transport 的实现：

``checksiteconfig -> hsw(req) -> hsw(1, msgpack(body)) -> ExtType(18)
-> getcaptcha -> hsw(0, response)``

PoW 和响应加解密仍由 hCaptcha 下发的 ``hsw.js`` 完成；本模块不伪造
``P1_`` token，也不调用第三方打码服务。Stripe Elements 只需要把返回的
token 放进 ``passive_captcha_token``。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote


SITEKEY = "463b917e-e264-403f-ad34-34af0ee10294"
DEFAULT_HOST = "js.stripe.com"
# 2026-09-03 UPI.har 中 checksiteconfig 使用的 asset 版本。服务端会在
# JWT 的 ``l`` 字段返回真正的当前版本；这个值只用于 csc 的初始路由，
# 可通过 UPI_HCAPTCHA_ASSET_V 覆盖。
DEFAULT_ASSET_V = "e426e928545a4a4671037b9367d8ae351ab7a6c0732137f9a97b00bce18205ce"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _node_bin() -> str:
    """Resolve Node without assuming a developer-machine PATH."""
    for value in (
        os.environ.get("UPI_HCAPTCHA_NODE_BIN"),
        os.environ.get("NODE"),
        shutil.which("node"),
    ):
        if value and str(value).strip():
            return str(value).strip()
    return "node"


def _helper_dir() -> Path:
    # core/ -> freepp-backend/ba_paypal/ba_fp_helpers/
    return Path(__file__).resolve().parents[1] / "ba_paypal" / "ba_fp_helpers"


def _node_env(helper_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    paths = [str(helper_dir), str(helper_dir.parent)]
    old = [p for p in env.get("NODE_PATH", "").split(os.pathsep) if p]
    env["NODE_PATH"] = os.pathsep.join(dict.fromkeys(old + paths))
    return env


def _new_curl_session(preferred: str = "", *, respect_env: bool = True) -> tuple[Any, str]:
    """Create a curl_cffi session using the first profile supported at runtime.

    The project is deployed with several curl_cffi versions; a newer Chrome
    label can exist in the HAR while the installed wheel only knows older
    labels. Keep the fallback bounded and report the selected profile.
    """
    from curl_cffi import requests as curl_requests

    requested = str(
        preferred or (os.environ.get("UPI_HCAPTCHA_IMPERSONATE") if respect_env else "") or ""
    ).strip()
    candidates = [requested, "chrome136", "chrome131", "chrome124", "chrome116", "chrome"]
    errors: list[str] = []
    for name in dict.fromkeys(x for x in candidates if x):
        try:
            return curl_requests.Session(impersonate=name), name
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")
    raise RuntimeError("no supported curl_cffi profile (" + ",".join(errors) + ")")


def _b64url_json(segment: str) -> dict[str, Any]:
    pad = "=" * ((4 - len(segment) % 4) % 4)
    raw = base64.urlsafe_b64decode(segment + pad)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("hCaptcha JWT payload is not an object")
    return value


def _jwt_payload(req: str) -> dict[str, Any]:
    parts = str(req or "").split(".")
    if len(parts) < 2:
        raise ValueError("checksiteconfig did not return a JWT req")
    return _b64url_json(parts[1])


def _hsw_url(req: str, *, asset_v: str = "") -> tuple[str, str]:
    payload = _jwt_payload(req)
    location = str(payload.get("l") or "").strip()
    if not location:
        raise ValueError("hCaptcha req payload has no asset location")
    if location.startswith("http://") or location.startswith("https://"):
        url = location.rstrip("/") + "/hsw.js"
    else:
        url = "https://newassets.hcaptcha.com" + "/" + location.lstrip("/") + "/hsw.js"
    version = str(asset_v or "").strip()
    if not version:
        match = re.search(r"/c/([0-9a-f]{20,})", location, re.I)
        version = match.group(1) if match else ""
    return url, version


def _json_stdout(stdout: str) -> dict[str, Any]:
    # Helpers may print diagnostics before the final JSON line.
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _token_from_json(value: Mapping[str, Any]) -> str:
    for key in ("generated_pass_UUID", "token", "passive_captcha_token"):
        token = str(value.get(key) or "").strip()
        if len(token) > 20:
            return token
    return ""


def _post_csc(
    session: Any,
    *,
    site_key: str,
    host: str,
    asset_v: str,
    spst: str,
    proxy: str | None,
    timeout: int,
) -> tuple[dict[str, Any], str]:
    """Fetch a fresh challenge, trying at most the two protocol variants seen in HARs."""
    query = (
        f"host={quote(host, safe='')}&sitekey={quote(site_key, safe='')}"
        f"&sc=1&swa=1&spst={quote(spst, safe='')}"
    )
    if asset_v:
        query = f"v={quote(asset_v, safe='')}&" + query
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://newassets.hcaptcha.com",
        "Referer": "https://newassets.hcaptcha.com/",
    }
    last_error = "checksiteconfig failed"
    for base in (
        "https://api.hcaptcha.com/checksiteconfig",
        "https://hcaptcha.com/checksiteconfig",
    ):
        try:
            response = session.post(
                f"{base}?{query}",
                headers=headers,
                data="{}",
                proxy=proxy,
                timeout=min(timeout, 40),
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if status != 200:
                last_error = f"csc_http_{status}"
                continue
            body = response.json() if hasattr(response, "json") else json.loads(response.text or "{}")
            challenge = body.get("c") if isinstance(body, dict) else None
            if isinstance(challenge, dict) and str(challenge.get("req") or "").strip():
                return challenge, ""
            last_error = "csc_no_req"
        except Exception as exc:  # pragma: no cover - exercised by live transport
            last_error = f"csc_{type(exc).__name__}:{exc}"
    return {}, last_error


def mint_hcaptcha_passive_protocol(
    *,
    site_key: str = SITEKEY,
    host: str = DEFAULT_HOST,
    proxy: str | None = None,
    user_agent: str = DEFAULT_UA,
    hl: str = "en",
    timeout: int = 90,
    asset_v: str = "",
    session: Any | None = None,
    node_runner: Callable[..., Any] | None = None,
    on_stage: Callable[[str, str, str], Any] | None = None,
    impersonate: str = "",
) -> dict[str, Any]:
    """Solve one fresh hCaptcha passive challenge through the HTTP protocol.

    ``session`` and ``node_runner`` are optional test seams. A successful result
    contains ``ok``, ``token`` and ``source=protocol``; no token is cached because
    Stripe binds it to the current Elements session and proxy fingerprint.
    Stripe may return HTTP-200 ``success:false`` for a Node-only fingerprint;
    that is reported as ``getcaptcha_soft_reject`` so the caller can switch to
    its browser fallback without treating transport as broken.
    """
    started = time.monotonic()

    def stage(name: str, status: str, detail: str) -> None:
        if callable(on_stage):
            try:
                on_stage(name, status, detail)
            except Exception:
                pass

    site_key = str(site_key or SITEKEY).strip()
    host = str(host or DEFAULT_HOST).strip().lower()
    ua = str(user_agent or DEFAULT_UA).strip()
    out: dict[str, Any] = {
        "ok": False,
        "token": "",
        "source": "protocol",
        "site_key": site_key,
        "host": host,
        "n_len": 0,
        "packed_len": 0,
    }
    if not _UUID_RE.fullmatch(site_key):
        out["error"] = "invalid_site_key"
        return out
    if not host or any(ch in host for ch in " /\\?&#"):
        out["error"] = "invalid_host"
        return out

    try:
        if session is None:
            session, selected_profile = _new_curl_session(impersonate)
            out["impersonate"] = selected_profile
    except Exception as exc:
        out["error"] = f"curl_cffi:{exc}"
        return out

    chosen_asset_v = str(
        asset_v or os.environ.get("UPI_HCAPTCHA_ASSET_V") or DEFAULT_ASSET_V
    ).strip()
    spst = str(os.environ.get("UPI_HCAPTCHA_SPST") or "1").strip()
    challenge, error = _post_csc(
        session,
        site_key=site_key,
        host=host,
        asset_v=chosen_asset_v,
        spst=spst,
        proxy=proxy,
        timeout=timeout,
    )
    # curl_cffi accepts unknown impersonation labels at Session construction
    # time and only raises when the first request is sent.  If an old runtime
    # is configured with a newer HAR label (for example chrome149), retry CSC
    # once with the bounded fallback profile instead of surfacing the raw
    # ``csc_ImpersonateError``.
    if not challenge and "ImpersonateError" in str(error):
        try:
            session, selected_profile = _new_curl_session("", respect_env=False)
            out["impersonate"] = selected_profile
            challenge, error = _post_csc(
                session,
                site_key=site_key,
                host=host,
                asset_v=chosen_asset_v,
                spst=spst,
                proxy=proxy,
                timeout=timeout,
            )
        except Exception as exc:
            error = f"csc_{type(exc).__name__}:{exc}"
    if not challenge:
        stage("checksiteconfig", "warn", error)
        out["error"] = error
        out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return out

    req = str(challenge["req"])
    stage("checksiteconfig", "ok", f"req_len={len(req)}")
    try:
        hsw_url, jwt_v = _hsw_url(req, asset_v=chosen_asset_v)
    except Exception as exc:
        stage("hsw", "warn", str(exc)[:120])
        out["error"] = f"req:{exc}"
        return out
    # csc 初始查询可以使用 fallback，但 hsw 加密 body 必须携带 JWT 中
    # 实际下发的版本，否则 getcaptcha 会把 PoW 视为过期。
    chosen_asset_v = jwt_v or chosen_asset_v
    helper_dir = _helper_dir()
    helper = helper_dir / "_jsdom_mint_unified.js"
    decrypt_helper = helper_dir / "hsw_decrypt_resp_node.js"
    if not helper.exists() or not decrypt_helper.exists():
        stage("hsw", "warn", "protocol_helpers_missing")
        out["error"] = "protocol_helpers_missing"
        return out

    with tempfile.TemporaryDirectory(prefix="upi_hcaptcha_") as tmp:
        root = Path(tmp)
        hsw_path = root / "hsw.js"
        try:
            response = session.get(
                hsw_url,
                headers={"User-Agent": ua, "Accept": "*/*", "Referer": "https://newassets.hcaptcha.com/"},
                proxy=proxy,
                timeout=min(timeout, 60),
            )
            if int(getattr(response, "status_code", 0) or 0) != 200 or not getattr(response, "content", b""):
                stage("hsw", "warn", f"HTTP {getattr(response, 'status_code', 0)}")
                out["error"] = f"hsw_http_{getattr(response, 'status_code', 0)}"
                return out
            hsw_path.write_bytes(bytes(response.content))
            stage("hsw", "ok", f"asset_bytes={len(response.content)}")
        except Exception as exc:
            stage("hsw", "warn", str(exc)[:120])
            out["error"] = f"hsw:{type(exc).__name__}:{exc}"
            return out

        input_data = {
            "req": req,
            "cObj": {"type": "hsw", "req": req},
            "sitekey": site_key,
            "host": host,
            "userAgent": ua,
            "hswPath": str(hsw_path),
            "v": chosen_asset_v or "1",
            "hl": hl or "en",
        }
        try:
            if node_runner is not None:
                pow_data = node_runner(helper, input_data, helper_dir, timeout)
            else:
                proc = subprocess.run(
                    [_node_bin(), str(helper)],
                    input=json.dumps(input_data, ensure_ascii=False),
                    text=True,
                    capture_output=True,
                    timeout=max(60, min(int(timeout), 180)),
                    env=_node_env(helper_dir),
                    cwd=str(helper_dir),
                )
                pow_data = _json_stdout(proc.stdout)
                helper_stderr = (proc.stderr or "").strip()
                if not pow_data and helper_stderr:
                    pow_data = {"ok": False, "error": helper_stderr[-500:]}
            if not isinstance(pow_data, dict) or not pow_data.get("ok"):
                raw_detail = str((pow_data or {}).get("error") or "helper_failed")
                # hsw.js syntax errors often include the whole minified source;
                # retain the tail where Node prints the actual exception.
                detail = raw_detail[-500:]
                stage("pow", "warn", detail[-240:])
                out["error"] = f"pow:{detail}"
                return out
        except Exception as exc:
            stage("pow", "warn", str(exc)[:120])
            out["error"] = f"pow:{type(exc).__name__}:{exc}"
            return out

        n = str(pow_data.get("n") or pow_data.get("proof") or "").strip()
        packed_b64 = str(pow_data.get("packed_b64") or "").strip()
        if not n or len(n) < 20:
            stage("pow", "warn", "pow_no_n")
            out["error"] = "pow_no_n"
            return out
        if not packed_b64:
            stage("pow", "warn", "pow_no_packed_payload")
            out["error"] = "pow_no_packed_payload"
            return out
        try:
            packed = base64.b64decode(packed_b64, validate=True)
        except Exception as exc:
            stage("pow", "warn", str(exc)[:120])
            out["error"] = f"pow_bad_payload:{exc}"
            return out
        out["n_len"] = len(n)
        out["packed_len"] = len(packed)
        stage("pow", "ok", f"n_len={len(n)} packed_len={len(packed)}")
        if pow_data.get("host_sum") is not None:
            out["host_sum"] = pow_data.get("host_sum")
        if pow_data.get("ms") is not None:
            out["pow_ms"] = pow_data.get("ms")

        try:
            gc = session.post(
                f"https://api.hcaptcha.com/getcaptcha/{site_key}",
                headers={
                    "User-Agent": ua,
                    "Origin": "https://newassets.hcaptcha.com",
                    "Referer": "https://newassets.hcaptcha.com/",
                    "Accept": "application/json, application/octet-stream",
                    "Content-Type": "application/octet-stream",
                },
                data=packed,
                proxy=proxy,
                timeout=min(timeout, 45),
            )
        except Exception as exc:
            stage("getcaptcha", "warn", str(exc)[:120])
            out["error"] = f"getcaptcha:{type(exc).__name__}:{exc}"
            return out

        content = bytes(getattr(gc, "content", b"") or b"")
        content_type = str(getattr(gc, "headers", {}).get("content-type", "") or "").lower()
        if int(getattr(gc, "status_code", 0) or 0) != 200:
            stage("getcaptcha", "warn", f"HTTP {getattr(gc, 'status_code', 0)}")
            out["error"] = f"getcaptcha_http_{getattr(gc, 'status_code', 0)}"
            return out
        if "json" in content_type or content.lstrip().startswith(b"{"):
            try:
                body = gc.json() if hasattr(gc, "json") else json.loads(content.decode())
            except Exception:
                body = {}
            token = _token_from_json(body if isinstance(body, dict) else {})
            if token:
                stage("getcaptcha", "ok", f"HTTP 200 JSON token_len={len(token)}")
                out.update(ok=True, token=token, elapsed_ms=int((time.monotonic() - started) * 1000))
                return out
            out["error"] = "getcaptcha_soft_reject"
            stage("getcaptcha", "warn", "soft_reject")
            return out
        if not content:
            stage("getcaptcha", "warn", "empty_response")
            out["error"] = "getcaptcha_empty_response"
            return out
        stage("getcaptcha", "ok", f"HTTP 200 binary_bytes={len(content)}")

        resp_path = root / "response.bin"
        resp_path.write_bytes(content)
        decrypt_input = {
            "respPath": str(resp_path),
            "hswPath": str(hsw_path),
            "sitekey": site_key,
            "host": host,
        }
        try:
            if node_runner is not None:
                decrypted = node_runner(decrypt_helper, decrypt_input, helper_dir, timeout)
            else:
                proc = subprocess.run(
                    [_node_bin(), str(decrypt_helper)],
                    input=json.dumps(decrypt_input, ensure_ascii=False),
                    text=True,
                    capture_output=True,
                    timeout=max(60, min(int(timeout), 180)),
                    env=_node_env(helper_dir),
                    cwd=str(helper_dir),
                )
                decrypted = _json_stdout(proc.stdout)
                helper_stderr = (proc.stderr or "").strip()
                if not decrypted and helper_stderr:
                    decrypted = {"ok": False, "error": helper_stderr[-500:]}
        except Exception as exc:
            stage("decrypt", "warn", str(exc)[:120])
            out["error"] = f"decrypt:{type(exc).__name__}:{exc}"
            return out
        token = _token_from_json(decrypted if isinstance(decrypted, dict) else {})
        if not token:
            stage("decrypt", "warn", str((decrypted or {}).get("error") or "no_token")[:120])
            out["error"] = f"decrypt_no_token:{(decrypted or {}).get('error') or 'unknown'}"
            return out
        out.update(ok=True, token=token, elapsed_ms=int((time.monotonic() - started) * 1000))
        stage("decrypt", "ok", f"token_len={len(token)}")
        return out


__all__ = ["SITEKEY", "DEFAULT_HOST", "mint_hcaptcha_passive_protocol"]
