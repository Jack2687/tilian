"""curl_cffi transport bridge for sentinel_bridge.js on Windows/SOCKS proxies.

两种运行模式（协议完全一致：stdin 读 JSON → stdout 写 JSON）：
  · 一次性（默认）：每次请求冷启动一个进程（历史行为）。
  · 常驻（SENTINEL_CURL_DAEMON_SOCKET=/path/a.sock[,/path/b.sock]）：
    进程复用，省掉每次 ~32MB 的内存与 curl_cffi 导入开销。
    单进程串行处理请求，可起多个实例、由调用方轮询。
"""
from __future__ import annotations

import json
import os
import sys

from curl_cffi import requests


def handle(payload: dict) -> dict:
    """与历史实现逐字段一致的请求逻辑。"""
    response = requests.request(
        method=str(payload.get("method") or "GET"),
        url=str(payload.get("url") or ""),
        headers={str(k): str(v) for k, v in (payload.get("headers") or {}).items()},
        data=payload.get("body") if payload.get("body") is not None else None,
        proxy=str(payload.get("proxy") or "") or None,
        timeout=float(payload.get("timeout") or 120),
        impersonate="chrome136",
    )
    return {
        "status": int(response.status_code),
        "body": response.text,
    }


def main() -> None:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    sys.stdout.write(json.dumps(handle(payload), ensure_ascii=False))


def serve(socket_path: str) -> None:
    """常驻模式：串行处理连接，每个连接一问一答。"""
    import socket as _socket

    directory = os.path.dirname(socket_path)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except Exception:
            pass
    try:
        if os.path.exists(socket_path):
            os.unlink(socket_path)
    except Exception:
        pass

    server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    server.bind(socket_path)
    try:
        os.chmod(socket_path, 0o660)
    except Exception:
        pass
    server.listen(64)
    sys.stderr.write("[sentinel_curl_daemon] listening %s pid=%d\n" % (socket_path, os.getpid()))
    sys.stderr.flush()

    while True:
        conn = None
        try:
            conn, _ = server.accept()
            buf = b""
            while True:
                piece = conn.recv(65536)
                if not piece:
                    break
                buf += piece
            try:
                result = handle(json.loads(buf.decode("utf-8")))
            except Exception as exc:                      # 单次失败只影响该请求
                result = {"error": str(exc)[:300]}
            conn.sendall(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except Exception:
            pass
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    _sock = str(os.environ.get("SENTINEL_CURL_DAEMON_SOCKET") or "").split(",")[0].strip()
    _has_unix = hasattr(__import__("socket"), "AF_UNIX")
    if _sock and _has_unix:
        serve(_sock)
    else:
        main()
