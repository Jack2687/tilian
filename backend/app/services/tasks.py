from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..card_sync import resolve_card
from ..models import OutboxEvent, QuotaCard, QuotaLedger, RuntimeConfig, Task, TaskItem, User, utcnow
from ..proxy import format_proxy_pool, normalize_proxy_pool
from ..schemas import TaskCreate
from ..security import encrypt_account_credential, mask_owner, mask_token, owner_key
from .concurrency import effective_concurrency, load_concurrency_config


MODE_NAMES = {
    "paypal": "PayPal", "gopay": "GoPay", "gcash": "GCash", "momo": "MoMo",
    "upi": "UPI", "blik": "BLIK", "pix": "PIX", "ideal": "iDEAL", "card": "直卡",
}


def iso_ms(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value else None


def task_proxy_pools(data: TaskCreate, config: dict[str, Any] | None = None) -> tuple[str, str]:
    """Resolve the chosen pools without silently changing a task to direct access."""
    primary_text = data.proxiesText
    exit_text = data.exitProxiesText
    if data.proxyMode == "builtin":
        channels = config.get("channels", {}) if isinstance(config, dict) else {}
        channel = channels.get(data.mode, {}) if isinstance(channels, dict) else {}
        if not isinstance(channel, dict) or channel.get("enabled") is not True:
            raise ValueError("该通道的管理员代理池未开启，请配置代理池或使用自定义代理")
        primary_text = str(channel.get("primaryText") or "")
        exit_text = str(channel.get("exitText") or "")
    primary = normalize_proxy_pool(primary_text, "建单代理池")
    if not primary:
        raise ValueError("建单代理池不能为空：提链必须指定建单代理，请配置管理员代理池或填写自定义代理")
    exits = normalize_proxy_pool(exit_text, "出口代理池")
    return format_proxy_pool(primary), format_proxy_pool(exits)


def snapshot(task: Task) -> dict[str, Any]:
    success = sum(item.status in {"success", "plus_success"} for item in task.items)
    failed = sum(item.status in {"failed", "timed_out", "canceled"} for item in task.items)
    running = sum(item.status in {"running", "waiting_blik", "waiting_plus"} for item in task.items)
    done = success + failed
    total = len(task.items)
    return {
        "id": task.id,
        "uid": task.owner_mask,
        "mode": task.mode,
        "modeName": task.mode_name,
        "status": task.status,
        "concurrency": task.concurrency,
        "createdAt": iso_ms(task.created_at),
        "finishedAt": iso_ms(task.finished_at),
        "progress": {
            "total": total, "done": done, "running": running,
            "queued": sum(item.status == "queued" for item in task.items),
            "success": success, "failed": failed,
            "percent": round(done / total * 100) if total else 0,
        },
        "items": [{
            "index": item.item_index,
            "email": item.email,
            "atMask": item.at_mask,
            "status": item.status,
            "stage": item.stage,
            "stageDetail": item.stage_detail,
            "attempts": item.attempt,
            "link": item.link,
            "error": item.error,
            "errorCode": item.error_code,
            "startedAt": iso_ms(item.started_at),
            "finishedAt": iso_ms(item.finished_at),
            "extra": item.result,
        } for item in sorted(task.items, key=lambda candidate: candidate.item_index)],
        "logs": [],
    }


class TaskService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()

    async def create(self, data: TaskCreate, request_id: str, user: User | None = None) -> dict[str, Any]:
        existing = await self.session.scalar(select(Task).where(Task.request_id == request_id))
        if existing:
            return snapshot(existing)
        accounts = data.accounts
        if not accounts and data.atsText.strip():
            from ..schemas import AccountInput
            accounts = [AccountInput(at=line.strip()) for line in data.atsText.splitlines() if line.strip()]
        if not accounts:
            raise ValueError("请先导入账号")
        # Reject an unavailable selected pool before adopting/binding a CDK or
        # reserving quota. An empty pool used to turn builtin tasks into direct
        # requests, so wallet requests failed upstream instead of at admission.
        proxy_config = await self.session.get(RuntimeConfig, "proxy_pools") if data.proxyMode == "builtin" else None
        proxies_text, exit_proxies_text = task_proxy_pools(data, proxy_config.value if proxy_config else None)
        concurrency_limit = effective_concurrency(await load_concurrency_config(self.session))
        if data.concurrency > concurrency_limit:
            raise ValueError(f"当前用户并发上限为 {concurrency_limit}")
        task_id = uuid.uuid4().hex[:16]
        cdk = data.cardCode.strip().upper()
        card, _ = await resolve_card(self.session, cdk, for_update=True)
        if not card or card.status != "active":
            raise ValueError("卡密不存在或已停用")
        if user:
            if card.owner_user_id is not None and card.owner_user_id != user.id:
                raise ValueError("该 CDK 已绑定其他用户")
            if card.owner_user_id is None:
                card.owner_user_id = user.id
                card.bound_at = utcnow()
            if len(accounts) > user.max_accounts_per_task:
                raise ValueError(f"当前用户单任务最多提交 {user.max_accounts_per_task} 个账号")
        available = card.quota - card.used_quota - card.reserved_quota
        if available < len(accounts):
            raise ValueError(f"卡密剩余额度不足，需要 {len(accounts)} 次，当前可用 {available} 次")
        card.reserved_quota += len(accounts)
        card.version += 1
        try:
            raw_params = json.loads(data.paramsText) if data.paramsText.strip() else {}
        except (TypeError, ValueError) as exc:
            raise ValueError("提链参数必须是有效 JSON 对象") from exc
        if not isinstance(raw_params, dict):
            raise ValueError("提链参数必须是 JSON 对象")
        # Persist a fresh per-task params object.  In particular, absent or
        # string-valued force flags remain false in the adapter's strict
        # boolean checks and cannot inherit state from another task.
        task_params = dict(raw_params)
        public_payload = {
            "proxyMode": data.proxyMode,
            "proxiesText": proxies_text,
            "exitProxiesText": exit_proxies_text,
            "params": task_params,
        }
        task = Task(
            id=task_id,
            owner_key=owner_key(cdk),
            owner_mask=mask_owner(cdk),
            owner_value=cdk,
            user_id=user.id if user else None,
            mode=data.mode,
            mode_name=MODE_NAMES[data.mode],
            status="queued",
            request_id=request_id,
            concurrency=data.concurrency,
            payload=public_payload,
        )
        for index, account in enumerate(accounts):
            task.items.append(TaskItem(
                item_index=index,
                email=account.email,
                at_mask=mask_token(account.at),
                credential=encrypt_account_credential(account.at, account.sessionToken),
                status="queued",
            ))
        event_id = uuid.uuid4().hex
        command = {
            "event_id": event_id,
            "event_type": "task.created.v1",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": request_id,
            "task_id": task_id,
            "payload": {"item_count": len(accounts), "mode": data.mode},
        }
        self.session.add(task)
        self.session.add(QuotaLedger(
            request_id=request_id,
            owner_key=owner_key(cdk),
            task_id=task_id,
            item_index=None,
            delta=len(accounts),
            kind="reserve",
        ))
        self.session.add(OutboxEvent(
            event_id=event_id,
            topic=self.settings.kafka_command_topic,
            message_key=task_id,
            payload=command,
        ))
        await self.session.commit()
        await self.session.refresh(task)
        return snapshot(task)

    async def list(self, cdk: str, limit: int = 30) -> list[dict[str, Any]]:
        result = await self.session.scalars(
            select(Task).where(Task.owner_key == owner_key(cdk)).order_by(Task.created_at.desc()).limit(limit)
        )
        return [snapshot(task) for task in result.unique().all()]

    async def get(self, task_id: str, cdk: str) -> dict[str, Any] | None:
        task = await self.session.scalar(select(Task).where(Task.id == task_id, Task.owner_key == owner_key(cdk)))
        return snapshot(task) if task else None

    async def cancel(self, task_id: str, cdk: str) -> dict[str, Any] | None:
        task = await self.session.scalar(select(Task).where(Task.id == task_id, Task.owner_key == owner_key(cdk)))
        if not task:
            return None
        # Use fresh locked rows: a worker may have finished since the task was
        # loaded. Match its item-before-task lock order to avoid deadlocks.
        rows = (await self.session.scalars(select(TaskItem).where(TaskItem.task_id == task_id)
            .order_by(TaskItem.item_index).with_for_update().execution_options(populate_existing=True))).all()
        active = [item for item in rows if item.status in {"queued", "running", "waiting_blik", "waiting_plus"}]
        if not active:
            await self.session.refresh(task, ["status", "finished_at"])
            return snapshot(task)
        finished_at = datetime.now(timezone.utc)
        card = await self.session.scalar(select(QuotaCard).where(QuotaCard.code_hash == task.owner_key)
            .with_for_update().execution_options(populate_existing=True))
        for item in active:
            request_id = f"{task.id}:{item.item_index}"
            settled = await self.session.scalar(select(QuotaLedger.id).where(
                QuotaLedger.request_id == request_id, QuotaLedger.kind.in_(["commit", "release"]),
            ))
            if settled is None:
                if card:
                    card.reserved_quota = max(0, card.reserved_quota - 1)
                    card.version += 1
                self.session.add(QuotaLedger(request_id=request_id, owner_key=task.owner_key,
                    task_id=task.id, item_index=item.item_index, delta=1, kind="release"))
            item.status = "canceled"
            item.error = "任务被取消"
            item.finished_at = finished_at
            item.result = {**(item.result or {}), "qrPending": False, "qrRefreshActive": False,
                "qr_pending": False, "qr_refresh_active": False}
        task.status = "canceled"
        task.finished_at = finished_at
        await self.session.commit()
        return snapshot(task)
