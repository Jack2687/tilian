#!/usr/bin/env python3
"""Probe proxy reachability in both curl_cffi and Chromium without logging credentials."""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import quote

from curl_cffi import requests
from playwright.sync_api import sync_playwright


def normalize(raw: str) -> str:
    value = str(raw or "").strip()
    if "://" in value:
        return value
    host, port, user, password = value.split(":", 3)
    return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"


def playwright_proxy(raw: str) -> dict:
    host, port, user, password = str(raw or "").strip().split(":", 3)
    return {
        "server": f"http://{host}:{port}",
        "username": user,
        "password": password,
    }


def probe(index: int, raw: str) -> dict:
    result = {
        "proxy": index,
        "http_ok": False,
        "browser_ok": False,
        "attestation_seen": False,
        "http_status": None,
        "browser_status": None,
        "error": "",
    }
    proxy_url = normalize(raw)
    try:
        response = requests.get(
            "https://chatgpt.com/?promo_campaign=plus-1-month-free",
            proxies={"http": proxy_url, "https": proxy_url},
            impersonate="chrome136",
            timeout=15,
        )
        result["http_status"] = response.status_code
        result["http_ok"] = response.status_code < 500
    except Exception as exc:
        result["error"] = "http:" + type(exc).__name__ + ":" + str(exc)[:100]

    try:
        executable = os.environ.get("GCASH_CHROME") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                headless=True,
                executable_path=executable,
                proxy=playwright_proxy(raw),
                args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"
                ),
                locale="en-PH",
                timezone_id="Asia/Manila",
            )

            def observe(request) -> None:
                try:
                    headers = request.all_headers()
                except Exception:
                    headers = request.headers or {}
                if headers.get("oai-web-deployment-attestation"):
                    result["attestation_seen"] = True

            context.on("request", observe)
            page = context.new_page()
            response = page.goto(
                "https://chatgpt.com/?promo_campaign=plus-1-month-free",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            result["browser_status"] = response.status if response else None
            result["browser_ok"] = bool(response and response.status < 400)
            try:
                page.wait_for_timeout(1500)
                page.evaluate(
                    """() => fetch('/backend-api/models?history_and_training_disabled=false', {
                      credentials: 'include', headers: {accept: 'application/json'}
                    }).then(r => r.text()).catch(() => '')"""
                )
                page.wait_for_timeout(2500)
            except Exception:
                pass
            browser.close()
    except Exception as exc:
        detail = "browser:" + type(exc).__name__ + ":" + str(exc).replace("\n", " ")[:140]
        result["error"] = (result["error"] + ";" if result["error"] else "") + detail
    return result


def main() -> int:
    raw_items = json.loads(os.environ.get("GCASH_PROXY_PROBE_JSON", "[]"))
    for index, raw in enumerate(raw_items, 1):
        print(json.dumps(probe(index, raw), ensure_ascii=False), flush=True)
        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
