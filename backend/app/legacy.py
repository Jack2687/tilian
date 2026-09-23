from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from .config import get_settings


class LegacyClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = self.settings.legacy_api_url.rstrip("/") + path
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.request(method, url, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail="旧执行服务暂不可用") from exc
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise HTTPException(status_code=response.status_code, detail=body.get("error") or "旧执行服务请求失败")
        return response.json()

    async def meta(self) -> Any:
        return await self.request("GET", "/api/meta")

    async def stats(self) -> Any:
        return await self.request("GET", "/api/stats")

    async def check_card(self, code: str) -> Any:
        return await self.request("POST", "/api/card/check", json={"code": code})

    async def parse_accounts(self, text: str) -> Any:
        return await self.request("POST", "/api/at/parse", json={"text": text})

    async def create_task(self, payload: dict[str, Any]) -> Any:
        return await self.request("POST", "/api/tasks", json=payload)

    async def list_tasks(self, card_code: str, limit: int) -> Any:
        return await self.request("GET", f"/api/tasks?limit={limit}", headers={"X-Tilian-CDK": card_code})

    async def get_task(self, task_id: str, card_code: str) -> Any:
        return await self.request("GET", f"/api/tasks/{quote(task_id)}", headers={"X-Tilian-CDK": card_code})

    async def cancel_task(self, task_id: str, card_code: str) -> Any:
        return await self.request("DELETE", f"/api/tasks/{quote(task_id)}", headers={"X-Tilian-CDK": card_code})


legacy_client = LegacyClient()
