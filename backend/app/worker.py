from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aiokafka import AIOKafkaConsumer
from sqlalchemy import select

from .config import get_settings
from . import fingerprints
from .database import SessionLocal, create_schema, engine
from .engine_adapter import EngineAdapter
from .infra.cache import cache
from .infra.messaging import event_bus
from .models import QuotaCard, QuotaLedger, Task, TaskEvent, TaskItem
from .security import decrypt_account_credential
from .services.engine_sessions import persist_engine_event, pop_engine_control


settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("tilian.worker")
adapter = EngineAdapter()

TERMINAL_RESULT_CODES = {
    # `upi_promo_ineligible` is deliberately NOT terminal: promo eligibility is
    # strongly tied to the checkout proxy's exit, so the same account can be
    # eligible on one proxy and not on another. Treating it as terminal killed
    # accounts before a real Checkout was ever created. It now rotates proxy
    # and fingerprint like a transport failure, and the task-level retry budget
    # still bounds it.
    "upi_promo_already_redeemed", "already_redeemed",
    "already-redeemed", "upi_promo_not_applied", "upi_zero_amount_not_confirmed",
    "ideal_zero_amount_not_confirmed", "promo_not_applied", "promotion_not_applied",
    "wallet_method_unavailable", "wallet_method_unavailable_env",
    "upi_payment_setup_declined", "upi_risk_blocked",
    "upi_billing_invalid", "upi_tls_configuration_error",
    "upi_payment_canceled", "upi_checkout_expired",
}


def build_attempt_fingerprint(
    channel: str,
    country: str,
    email: str,
    credential: str,
    attempt: int,
) -> dict[str, Any]:
    """Create the browser identity used by one engine attempt.

    UPI runs the reference `upi-git5` rail, which is protocol-only: it presents
    a macOS Chrome/Firefox identity over curl_cffi and never opens a browser.
    The generic random kernel pool would pair a Windows UA with this rail's
    macOS-shaped risk context, so UPI rotates a fixed macOS profile set instead.
    """
    channel = str(channel or "").strip().lower()
    if channel == "upi":
        # Same four profiles as the reference UPI_GIT5_MACOS_PROFILES. The
        # reference picks two per job and only uses the second from attempt 2
        # on; this builder is stateless per attempt, so it walks the full set.
        target = fingerprints.UPI_GIT5_MACOS_PROFILES[(max(1, int(attempt)) - 1) % len(fingerprints.UPI_GIT5_MACOS_PROFILES)]
        kernel = "firefox" if target.startswith("firefox") else "chrome"
        return fingerprints.generate(
            country,
            channel=channel,
            kernel=kernel,
            impersonate=target,
            platform="macOS",
            # 该 rail 固定 en-IN；指纹随机池里的 hi-IN 会与其余身份字段矛盾。
            locale="en-IN",
        )
    if channel in {"ideal", "gcash"}:
        return fingerprints.generate(country, channel=channel, kernel="chrome")
    if attempt > 1:
        return fingerprints.generate(country, channel=channel)
    return fingerprints.get_or_create(country=country, email=email or "", at=credential, channel=channel)


def result_retryable(result: dict[str, Any]) -> bool:
    """Retry only transport/risk failures; promotion/payment capability states end now."""
    reason = str(result.get("reason_code") or "").strip().lower()
    error = str(result.get("error") or "").strip().lower()
    if reason in TERMINAL_RESULT_CODES or reason.endswith("_payment_method_unavailable"):
        return False
    if any(marker in error for marker in (
        "risk_blocked", "submission blocked", "result=blocked",
        "status=blocked", "http 403", "风控拦截",
    )):
        return False
    if any(marker in error for marker in (
        "优惠无效", "无优惠资格", "优惠未应用", "promotion not eligible",
        "promo not eligible", "already redeemed", "payment method unavailable",
        "支付方式不可用", "应付金额未压到 0", "amount is not zero",
    )):
        return False
    extra = result.get("extra") if isinstance(result.get("extra"), dict) else {}
    return bool(result.get("retryable") or extra.get("retryable"))


async def publish_event(task_id: str, item_index: int, event_type: str, payload: dict[str, Any]) -> None:
    event = {
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": task_id,
        "task_id": task_id,
        "item_index": item_index,
        "payload": payload,
    }
    await event_bus.publish(settings.kafka_event_topic, f"{task_id}:{item_index}", event)


async def settle_quota(task: Task, item: TaskItem, success: bool) -> None:
    async with SessionLocal() as session:
        card = await session.scalar(select(QuotaCard).where(QuotaCard.code_hash == task.owner_key).with_for_update())
        if not card:
            return
        request_id = f"{task.id}:{item.item_index}"
        kind = "commit" if success else "release"
        existing = await session.scalar(select(QuotaLedger).where(QuotaLedger.request_id == request_id, QuotaLedger.kind.in_(["commit", "release"])))
        if existing:
            return
        card.reserved_quota = max(0, card.reserved_quota - 1)
        if success:
            card.used_quota += 1
        card.version += 1
        session.add(QuotaLedger(request_id=request_id, owner_key=task.owner_key, task_id=task.id, item_index=item.item_index, delta=1, kind=kind))
        await session.commit()


async def execute_item(task_id: str, item_index: int, semaphore: asyncio.Semaphore) -> None:
    retry_again = False
    async with semaphore, cache.lease(f"task:{task_id}:{item_index}") as acquired:
        if not acquired:
            return
        async with SessionLocal() as session:
            item = await session.scalar(select(TaskItem).where(TaskItem.task_id == task_id, TaskItem.item_index == item_index))
            task = await session.scalar(select(Task).where(Task.id == task_id))
            if not task or not item or item.status not in {"queued", "running"}:
                return
            item.status = "running"
            item.stage = "start"
            item.stage_detail = "Worker 已领取任务"
            # A retry starts a fresh engine session; clear the previous
            # attempt's QR/page projection before publishing running state.
            item.link = None
            item.result = {}
            item.started_at = item.started_at or datetime.now(timezone.utc)
            item.attempt += 1
            task.status = "running"
            session.add(TaskEvent(
                event_id=uuid.uuid4().hex, task_id=task_id, item_index=item_index,
                event_type="task.item.started.v1",
                payload={
                    "attempt": item.attempt, "status": item.status,
                    "stage": item.stage, "detail": item.stage_detail,
                },
            ))
            await session.commit()
            credential, account_session_token = decrypt_account_credential(item.credential)
            pool = [line.strip() for line in str(task.payload.get("proxiesText") or "").splitlines() if line.strip()]
            exit_pool = [line.strip() for line in str(task.payload.get("exitProxiesText") or "").splitlines() if line.strip()]
            proxy_index = item.item_index + item.attempt - 1
            proxy = pool[proxy_index % len(pool)] if pool else ""
            exit_proxy = exit_pool[proxy_index % len(exit_pool)] if exit_pool else ""
            effective_payload = dict(task.payload or {})
            params = effective_payload.get("params")
            effective_payload["params"] = dict(params) if isinstance(params, dict) else {}
            if account_session_token:
                effective_payload["params"]["sessionToken"] = account_session_token
            channel = str(task.mode or "").strip().lower()
            raw_country = str(effective_payload["params"].get("country") or "").strip().upper()
            country = raw_country or {"gopay": "ID", "gcash": "PH", "momo": "VN", "upi": "IN", "blik": "PL", "ideal": "NL", "pix": "BR", "card": "PH"}.get(channel, "US")
            try:
                fingerprint = build_attempt_fingerprint(
                    channel, country, item.email or "", credential, item.attempt
                )
            except Exception:
                effective_payload["params"].pop("fingerprint", None)
                logger.warning("fingerprint generation failed mode=%s task_id=%s item=%s", channel, task_id, item_index, exc_info=True)
            else:
                effective_payload["params"]["fingerprint"] = fingerprint
            mode, email, payload, attempt = task.mode, item.email, effective_payload, item.attempt

        await publish_event(task_id, item_index, "task.item.started.v1", {"attempt": attempt})
        try:
            result, stages = await adapter.execute(
                mode, credential, email, proxy, payload, exit_proxy,
                on_event=lambda message: persist_engine_event(task_id, item_index, attempt, message),
                read_control=lambda: pop_engine_control(task_id, item_index, attempt),
            )
        except Exception as exc:
            logger.exception("engine execution failed task_id=%s item=%s", task_id, item_index)
            result, stages = {"ok": False, "error": f"Worker 执行异常: {str(exc)[:800]}", "reason_code": "worker_exception", "retryable": False}, [{"stage": "engine_exception", "detail": "提链引擎启动或执行异常"}]
        retryable = result_retryable(result)

        async with SessionLocal() as session:
            item = await session.scalar(select(TaskItem).where(TaskItem.task_id == task_id, TaskItem.item_index == item_index).with_for_update())
            task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if not task or not item or item.status == "canceled":
                return
            if stages:
                item.stage = stages[-1]["stage"]
                item.stage_detail = stages[-1]["detail"]
            elif result.get("ok"):
                item.stage = "completed"
                item.stage_detail = "执行完成"
            else:
                reason_code = str(result.get("reason_code") or "engine_execute")[:128]
                item.stage = {
                    "engine_timeout": "engine_timeout",
                    "engine_no_result": "engine_no_result",
                    "engine_dependency_missing": "engine_bootstrap",
                    "worker_exception": "engine_exception",
                }.get(reason_code, "engine_execute")
                item.stage_detail = str(result.get("error") or "提链引擎执行失败")[:500]
            max_retries = settings.upi_max_retries if channel == "upi" else settings.task_max_retries
            max_retries = max(1, min(5, int(max_retries)))
            if result.get("ok"):
                item.status = "success"
                item.stage = "completed"
                item.stage_detail = "提链成功"
                item.link = str(result.get("link") or "")
                # Keep the complete public engine result per item.  Storing
                # only ``extra`` used to discard channel metadata such as
                # payment method, checkout amount, promo state and provider
                # redirect URLs; concurrent tasks then appeared to share a
                # generic success with no channel details.  Exclude control
                # fields while retaining link/QR/payment metadata uniformly.
                public_result = {
                    key: value for key, value in result.items()
                    if key not in {"ok", "error", "retryable"}
                }
                result_extra = result.get("extra") if isinstance(result.get("extra"), dict) else {}
                # Replace the previous attempt's projection so a retry cannot
                # leak an old QR/session/payment page into this result.
                item.result = {**public_result, **result_extra}
                item.error = None
                item.error_code = None
                item.finished_at = datetime.now(timezone.utc)
                settle = True
            elif retryable and item.attempt < max_retries:
                item.status = "queued"
                item.error = str(result.get("error") or "执行失败")[:1000]
                item.error_code = str(result.get("reason_code") or "engine_failed")[:128]
                settle = None
            else:
                item.status = "failed"
                item.error = str(result.get("error") or "执行失败")[:1000]
                item.error_code = str(result.get("reason_code") or "engine_failed")[:128]
                item.finished_at = datetime.now(timezone.utc)
                settle = False
            if not result.get("ok"):
                item.link = None
                public_result = {
                    key: value for key, value in result.items()
                    if key not in {"ok", "error", "retryable"}
                }
                result_extra = result.get("extra") if isinstance(result.get("extra"), dict) else {}
                item.result = {**public_result, **result_extra, "qrPending": False, "qrRefreshActive": False}
            event_id = uuid.uuid4().hex
            session.add(TaskEvent(
                event_id=event_id, task_id=task_id, item_index=item_index,
                event_type="task.item.completed.v1",
                payload={
                    "ok": bool(result.get("ok")), "stage": item.stage,
                    "detail": item.stage_detail, "error_code": item.error_code,
                    "error": item.error, "attempt": item.attempt,
                    "status": item.status, "retry": item.status == "queued",
                },
            ))
            await session.commit()
            task_copy, item_copy = task, item

        if settle is not None:
            await settle_quota(task_copy, item_copy, settle)
        await publish_event(task_id, item_index, "task.item.completed.v1", {"ok": bool(result.get("ok")), "retry": settle is None})
        retry_again = settle is None
    if retry_again:
        await asyncio.sleep(min(30, 2 ** attempt))
        await execute_item(task_id, item_index, semaphore)


async def recompute_task(task_id: str) -> None:
    async with SessionLocal() as session:
        task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if not task or task.status == "canceled":
            return
        statuses = [item.status for item in task.items]
        if any(status in {"queued", "running", "waiting_blik", "waiting_plus"} for status in statuses):
            task.status = "running" if "running" in statuses else "queued"
        else:
            task.status = "success" if any(status in {"success", "plus_success"} for status in statuses) else "failed"
            task.finished_at = datetime.now(timezone.utc)
        await session.commit()


async def process_task(task_id: str) -> None:
    async with SessionLocal() as session:
        task = await session.scalar(select(Task).where(Task.id == task_id))
        if not task or task.status in {"success", "failed", "canceled"}:
            return
        indexes = [item.item_index for item in task.items if item.status == "queued"]
        concurrency = max(1, min(task.concurrency, 100))
    semaphore = asyncio.Semaphore(concurrency)
    await asyncio.gather(*(execute_item(task_id, index, semaphore) for index in indexes))
    await recompute_task(task_id)


async def consume() -> None:
    if not settings.kafka_enabled:
        raise RuntimeError("KAFKA_BOOTSTRAP_SERVERS is required for worker consume mode")
    await create_schema()
    await event_bus.start()
    consumer = AIOKafkaConsumer(
        settings.kafka_command_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers.split(","),
        group_id=settings.kafka_consumer_group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda value: json.loads(value.decode()),
    )
    await consumer.start()
    try:
        async for message in consumer:
            task_id = str(message.value.get("task_id") or "")
            if task_id:
                try:
                    await process_task(task_id)
                except Exception:
                    logger.exception("task processing failed task_id=%s", task_id)
                    continue
            await consumer.commit()
    finally:
        await consumer.stop()
        await event_bus.stop()
        await cache.close()
        await engine.dispose()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", default="", help="process one task id without Kafka")
    args = parser.parse_args()
    await create_schema()
    if args.once:
        await process_task(args.once)
        await engine.dispose()
    else:
        await consume()


if __name__ == "__main__":
    asyncio.run(main())
