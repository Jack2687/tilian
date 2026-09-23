"""代理地址归一化。

代理供应商常见的导出格式是 ``host:port:username:password``，而
httpx/requests/curl 需要带协议且认证信息位于 ``username:password@``。
所有进入任务队列的代理都经过这里转换，避免不同引擎各自解析产生差异。
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def _host_port(value: str) -> tuple[str, int]:
    text = value.strip()
    if text.startswith("[") and "]:" in text:
        host, port_text = text[1:].split("]:", 1)
        host = f"[{host}]"
    else:
        if ":" not in text:
            raise ValueError("代理缺少端口，例如 host:port:user:password")
        host, port_text = text.rsplit(":", 1)
    if not host or not port_text.isdigit():
        raise ValueError("代理主机或端口格式不正确")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("代理端口超出范围")
    return host, port


def _credentials(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise ValueError("代理凭据格式应为 username:password")
    username, password = value.split(":", 1)
    if not username or not password:
        raise ValueError("代理用户名或密码为空")
    return username, password


def _build(scheme: str, host: str, port: int, username: str = "", password: str = "") -> str:
    auth = ""
    if username or password:
        auth = f"{quote(username, safe='-._~')}:{quote(password, safe='-._~')}@"
    return f"{scheme}://{auth}{host}:{port}"


def normalize_proxy_url(raw: str, default_scheme: str = "http") -> str:
    """Return a canonical proxy URL or raise ``ValueError`` for bad input.

    Supported forms include:

    - ``http://user:pass@host:port``
    - ``user:pass@host:port``
    - ``host:port:user:pass`` (1024Proxy/Kookeey exports)
    - ``host:port``
    """

    value = str(raw or "").strip()
    if not value:
        return ""

    if _SCHEME_RE.match(value):
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in SUPPORTED_PROXY_SCHEMES:
            raise ValueError(f"代理协议 {scheme} 暂未支持")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("代理端口格式不正确") from exc
        if not parsed.hostname:
            raise ValueError("代理 URL 缺少主机")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        if port is None:
            # 允许管理员配置中的无端口占位地址（httpx 会使用协议默认端口）。
            auth = ""
            if parsed.username is not None:
                auth = f"{quote(unquote(parsed.username), safe='-._~')}:{quote(unquote(parsed.password or ''), safe='-._~')}@"
            return f"{scheme}://{auth}{host}"
        return _build(scheme, host, port, unquote(parsed.username or ""), unquote(parsed.password or ""))

    scheme = default_scheme.lower().strip() or "http"
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(f"代理协议 {scheme} 暂未支持")

    if value.count("@") == 1:
        left, right = value.split("@", 1)
        try:
            username, password = _credentials(left)
            host, port = _host_port(right)
        except ValueError:
            host, port = _host_port(left)
            username, password = _credentials(right)
        return _build(scheme, host, port, username, password)

    parts = value.split(":", 3)
    if len(parts) == 4 and parts[1].isdigit():
        host, port_text, username, password = parts
        host, port = _host_port(f"{host}:{port_text}")
        if not username or not password:
            raise ValueError("代理用户名或密码为空")
        return _build(scheme, host, port, username, password)

    host, port = _host_port(value)
    return _build(scheme, host, port)


def normalize_proxy_pool(raw: str | list[str] | tuple[str, ...], label: str = "代理池") -> list[str]:
    values = raw if isinstance(raw, (list, tuple)) else str(raw or "").replace("\r", "").split("\n")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(values, 1):
        value = str(item or "").strip()
        if not value:
            continue
        try:
            proxy = normalize_proxy_url(value)
        except ValueError as exc:
            raise ValueError(f"{label}第 {index} 条：{exc}") from exc
        if proxy not in seen:
            normalized.append(proxy)
            seen.add(proxy)
    return normalized


def format_proxy_pool(values: list[str]) -> str:
    return "\n".join(values)


__all__ = ["SUPPORTED_PROXY_SCHEMES", "format_proxy_pool", "normalize_proxy_pool", "normalize_proxy_url"]
