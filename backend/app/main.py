from __future__ import annotations

import logging
import asyncio
import base64
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from .config import get_settings
from .card_sync import resolve_card
from .database import create_schema, engine, get_session
from .infra.cache import cache
from .infra.messaging import event_bus
from .infra.outbox import relay_forever
from .legacy import legacy_client
from .models import AdminAnnouncement, AuditLog, CardFusion, QuotaCard, RuntimeConfig, Task, TaskEvent, TaskItem, User, utcnow
from .proxy import format_proxy_pool, normalize_proxy_pool, normalize_proxy_url
from .schemas import AccountParse, AdminCardStatusInput, AdminUserUpdate, AnnouncementInput, BlikCodeInput, CardCheck, CardFuseInput, CardGenerateInput, CardImport, ConcurrencyConfig, LoginInput, ProxyPoolsConfig, QrRefreshInput, RegisterInput, SUPPORTED_MODES, TaskCreate, UserPolicyConfig
from .security import card_hash, issue_user_token, mask_owner, owner_key, password_hash, password_verify, verify_user_token
from .services.tasks import TaskService
from .services.concurrency import effective_concurrency, load_concurrency_config
from .services.engine_sessions import queue_engine_control
from .engine_protocol import CONTROL_EVENT


settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("tilian.api")

REQUESTS = Counter("tilian_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("tilian_http_request_seconds", "HTTP latency", ["method", "route"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    outbox_stop = asyncio.Event()
    outbox_task: asyncio.Task[None] | None = None
    if settings.auto_create_schema:
        await create_schema()
    if settings.kafka_enabled:
        await event_bus.start()
        outbox_task = asyncio.create_task(relay_forever(outbox_stop), name="outbox-relay")
    yield
    outbox_stop.set()
    if outbox_task:
        await outbox_task
    await event_bus.stop()
    await cache.close()
    await engine.dispose()


app = FastAPI(
    title="DJB 提链 API",
    version="2.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Idempotency-Key", "X-Tilian-CDK", "X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.trace_id = trace_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        elapsed = time.perf_counter() - started
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        status = getattr(locals().get("response"), "status_code", 500)
        REQUESTS.labels(request.method, route_path, str(status)).inc()
        LATENCY.labels(request.method, route_path).observe(elapsed)
    response.headers["X-Request-ID"] = trace_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"code": "validation_error", "message": "请求参数不合法", "trace_id": request.state.trace_id, "details": {"errors": exc.errors()}},
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "请求失败"
    return JSONResponse(status_code=exc.status_code, content={"code": "http_error", "message": detail, "trace_id": request.state.trace_id})


@app.exception_handler(Exception)
async def unknown_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error trace_id=%s", request.state.trace_id)
    return JSONResponse(status_code=500, content={"code": "internal_error", "message": "服务器内部错误", "trace_id": request.state.trace_id})


@app.get("/health/live")
async def live() -> dict[str, Any]:
    return {"ok": True, "service": "api", "version": "2.0.0"}


def user_public(user: User, *, concurrency: int) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name or user.username,
        "role": user.role,
        "status": user.status,
        "taskConcurrency": concurrency,
        "maxAccountsPerTask": user.max_accounts_per_task,
        "allowFusion": user.allow_fusion,
        "notes": user.notes or "",
        "createdAt": user.created_at.isoformat(),
        "lastLoginAt": user.last_login_at.isoformat() if user.last_login_at else None,
    }


async def get_current_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = Depends(get_session),
) -> User:
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    claims = verify_user_token(supplied) if supplied else None
    if not claims:
        raise HTTPException(status_code=401, detail="请先登录")
    user = await session.get(User, claims["user_id"])
    if not user or user.token_version != claims["token_version"]:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号已被管理员停用")
    return user


def ensure_card_access(card: QuotaCard, user: User, *, bind: bool = True) -> None:
    if card.owner_user_id is not None and card.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="该 CDK 已绑定其他用户")
    if bind and card.owner_user_id is None:
        card.owner_user_id = user.id
        card.bound_at = utcnow()


@app.post("/api/v1/auth/register", status_code=201)
async def register(body: RegisterInput, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    policy = await session.get(RuntimeConfig, "user_policy")
    values = policy.value if policy and isinstance(policy.value, dict) else {}
    if values.get("registrationEnabled", True) is False:
        raise HTTPException(status_code=403, detail="当前暂未开放注册")
    user_count = int(await session.scalar(select(func.count()).select_from(User)) or 0)
    if user_count >= int(values.get("maxUsers", 10000)):
        raise HTTPException(status_code=403, detail="注册人数已达到上限")
    username = body.username.strip().lower()
    if await session.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(status_code=409, detail="用户名已存在")
    concurrency = effective_concurrency(await load_concurrency_config(session))
    user = User(
        username=username,
        password_hash=password_hash(body.password),
        display_name=body.displayName.strip() or username,
        task_concurrency=concurrency,
        max_accounts_per_task=int(values.get("maxAccountsPerTask", 100)),
        allow_fusion=bool(values.get("allowFusion", True)),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {"token": issue_user_token(user.id, user.token_version), "user": user_public(user, concurrency=concurrency)}


@app.post("/api/v1/auth/login")
async def login(body: LoginInput, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    username = body.username.strip().lower()
    user = await session.scalar(select(User).where(User.username == username))
    if not user or not password_verify(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号已被管理员停用")
    user.last_login_at = utcnow()
    await session.commit()
    concurrency = effective_concurrency(await load_concurrency_config(session))
    return {"token": issue_user_token(user.id, user.token_version), "user": user_public(user, concurrency=concurrency)}


@app.get("/api/v1/auth/me")
async def me(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    concurrency = effective_concurrency(await load_concurrency_config(session))
    return {"user": user_public(user, concurrency=concurrency)}


@app.get("/health/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    checks: dict[str, Any] = {"database": False, "redis": None, "kafka": None, "legacy": None}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:
        checks["database"] = str(exc)[:120]
    if settings.redis_enabled:
        try:
            checks["redis"] = await cache.ping()
        except Exception as exc:
            checks["redis"] = str(exc)[:120]
    if settings.kafka_enabled:
        try:
            checks["kafka"] = await event_bus.ping()
        except Exception as exc:
            checks["kafka"] = str(exc)[:120]
    if settings.execution_mode == "legacy":
        try:
            await legacy_client.meta()
            checks["legacy"] = True
        except HTTPException:
            checks["legacy"] = False
    required = [checks["database"] is True]
    if settings.execution_mode == "legacy":
        required.append(checks["legacy"] is True)
    if settings.execution_mode == "distributed":
        try:
            checks["workers"] = await asyncio.wait_for(event_bus.worker_status(), timeout=6)
        except Exception:
            checks["workers"] = {"ready": False, "state": "Unavailable", "members": 0}
        required.extend([checks["redis"] is True, checks["kafka"] is True])
        required.append(checks["workers"]["ready"] is True)
    return {"ready": all(required), "mode": settings.execution_mode, "checks": checks}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/v1/meta")
async def meta(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if settings.execution_mode == "legacy":
        return await legacy_client.meta()
    concurrency = await load_concurrency_config(session)
    limit = effective_concurrency(concurrency)
    proxy_config = await session.get(RuntimeConfig, "proxy_pools")
    proxy_channels = proxy_config.value.get("channels", {}) if proxy_config and isinstance(proxy_config.value, dict) else {}
    proxy_status = {}
    for mode in SUPPORTED_MODES:
        channel = proxy_channels.get(mode, {}) if isinstance(proxy_channels, dict) else {}
        primary_count = len([line for line in str(channel.get("primaryText") or "").splitlines() if line.strip()]) if isinstance(channel, dict) else 0
        exit_count = len([line for line in str(channel.get("exitText") or "").splitlines() if line.strip()]) if isinstance(channel, dict) else 0
        proxy_status[mode] = {"enabled": bool(channel.get("enabled") and primary_count) if isinstance(channel, dict) else False, "primaryCount": primary_count, "exitCount": exit_count}
    return {
        "app": "DJB提链",
        "version": "V2.0",
        "simMode": False,
        "modes": [
            {"id": key, "name": name, "description": "异步集群提链"}
            for key, name in (("paypal", "PayPal"), ("gopay", "GoPay"), ("gcash", "GCash"), ("momo", "MoMo"), ("upi", "UPI"), ("blik", "BLIK"), ("pix", "PIX"), ("ideal", "iDEAL"), ("card", "直卡"))
        ],
        "engine": {"defaultConcurrency": limit, "maxConcurrency": limit, "proxyPools": proxy_status, "ha": {"enabled": True, "mode": "distributed", "ready": True}},
        "limits": {"userConcurrency": limit, "maxConcurrency": limit},
        "serverTime": int(time.time() * 1000),
    }


@app.get("/api/v1/stats")
async def stats(mode: str | None = Query(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if settings.execution_mode == "legacy":
        data = await legacy_client.stats()
        return {
            **data,
            "totalLinkSuccess": data.get("totalLinkSuccess", data.get("totalLinks", 0)),
            "totalCallbackPaid": data.get("totalCallbackPaid", data.get("totalPlusPaid", 0)),
        }
    task_query = select(func.count()).select_from(Task)
    item_query = select(TaskItem).join(Task, Task.id == TaskItem.task_id)
    if mode and mode.strip():
        task_query = task_query.where(Task.mode == mode.strip().lower())
        item_query = item_query.where(Task.mode == mode.strip().lower())
    task_total = await session.scalar(task_query) or 0
    items = (await session.scalars(item_query)).all()
    success = sum(item.status in {"success", "plus_success"} for item in items)
    failed = sum(item.status in {"failed", "timed_out"} for item in items)
    denominator = success + failed
    return {"totalTasks": task_total, "totalLinkSuccess": success, "totalFailed": failed, "totalCallbackPaid": 0, "successRate": round(success / denominator * 100, 1) if denominator else 0}


@app.get("/api/v1/recent-success")
async def recent_success(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    bucket_ms = 30 * 60 * 1000
    start_ms = int(now.timestamp() * 1000) - 48 * bucket_ms
    modes = {"paypal": "PayPal", "gopay": "GoPay", "gcash": "GCash", "momo": "MoMo", "upi": "UPI", "blik": "BLIK", "pix": "PIX", "ideal": "iDEAL", "card": "直卡"}
    cells = [{"start": start_ms + i * bucket_ms, "end": start_ms + (i + 1) * bucket_ms, "total": 0, "byMode": {key: 0 for key in modes}} for i in range(48)]
    totals = {key: 0 for key in modes}
    result = await session.execute(select(TaskItem, Task).join(Task, Task.id == TaskItem.task_id).where(TaskItem.status.in_(["success", "plus_success"]), TaskItem.finished_at.is_not(None)))
    for item, task in result.all():
        index = (int(item.finished_at.timestamp() * 1000) - start_ms) // bucket_ms
        totals[task.mode] = totals.get(task.mode, 0) + 1
        if 0 <= index < len(cells):
            cells[index]["total"] += 1
            cells[index]["byMode"][task.mode] = cells[index]["byMode"].get(task.mode, 0) + 1
    return {"updatedAt": int(now.timestamp() * 1000), "buckets": 48, "modes": modes, "totals": {"all": sum(totals.values()), "byMode": totals}, "cells": cells}


@app.post("/api/v1/proxy/check")
async def check_proxy(body: dict[str, Any]) -> dict[str, Any]:
    """检查单条代理并返回出口 IP 与耗时，供用户端代理池逐条诊断。"""
    raw_proxy = str(body.get("proxy") or "").strip()
    if not raw_proxy:
        raise HTTPException(status_code=422, detail="代理地址不能为空")
    try:
        proxy = normalize_proxy_url(raw_proxy)
    except ValueError as exc:
        return {"ok": False, "proxy": raw_proxy, "latencyMs": 0, "error": f"代理格式错误：{exc}"}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=8.0) as client:
            response = await client.get("https://api.ipify.org?format=json")
            response.raise_for_status()
            payload = response.json()
        return {"ok": True, "proxy": raw_proxy, "normalizedProxy": proxy, "ip": str(payload.get("ip") or ""), "latencyMs": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "proxy": raw_proxy, "normalizedProxy": proxy, "latencyMs": round((time.perf_counter() - started) * 1000), "error": str(exc)[:240]}


@app.get("/api/v1/plus-records")
async def plus_records(limit: int = Query(default=30, ge=1, le=200), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (await session.scalars(select(TaskItem).where(TaskItem.status == "plus_success", TaskItem.finished_at.is_not(None)).order_by(TaskItem.finished_at.desc()).limit(limit))).all()
    return {"records": [{"email": row.email, "t": row.finished_at.isoformat() if row.finished_at else "", "taskId": row.task_id} for row in rows]}


@app.get("/api/v1/payment/config")
async def payment_config() -> dict[str, Any]:
    # 支付通道接入前也保留完整前端入口，避免旧版按钮消失；通过运行时配置开启真实档位。
    return {"enabled": False, "plans": []}


@app.post("/api/v1/payment/create")
async def payment_create() -> dict[str, Any]:
    raise HTTPException(status_code=503, detail="在线支付尚未配置，请联系管理员")


@app.get("/api/v1/payment/order")
async def payment_order() -> dict[str, Any]:
    raise HTTPException(status_code=404, detail="支付订单不存在")


@app.post("/api/v1/cards/check")
async def check_card(body: CardCheck, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if settings.execution_mode == "legacy":
        return await legacy_client.check_card(body.code)
    card, imported = await resolve_card(session, body.code)
    if not card or card.status != "active":
        raise HTTPException(status_code=400, detail="卡密不存在或已停用")
    ensure_card_access(card, user)
    remain = max(0, card.quota - card.used_quota - card.reserved_quota)
    if imported or card.owner_user_id == user.id:
        await session.commit()
    return {"ok": True, "code": card.code_mask, "quota": card.quota, "usedQuota": card.used_quota, "reservedQuota": card.reserved_quota, "remain": remain, "status": card.status}


def generate_card_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
    return "TL-" + "-".join(groups)


def is_expired(value: datetime | None) -> bool:
    if not value:
        return False
    current = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return current < datetime.now(timezone.utc)


@app.post("/api/v1/admin/cards/generate", status_code=201)
async def generate_admin_cards(body: CardGenerateInput, authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    created: list[dict[str, Any]] = []
    expires_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=body.days)
    for _ in range(body.count):
        code = generate_card_code()
        while await session.scalar(select(QuotaCard.id).where(QuotaCard.code_hash == card_hash(code))):
            code = generate_card_code()
        card = QuotaCard(code_hash=card_hash(code), code_mask=mask_owner(code), code_value=code, quota=body.quota, status="active", fusion_disabled=body.fusionDisabled, remark=body.remark, expires_at=expires_at)
        session.add(card)
        created.append({"code": code, "quota": body.quota, "days": body.days, "remark": body.remark, "fusionDisabled": body.fusionDisabled, "expiresAt": expires_at.isoformat()})
    await add_audit(session, "cards.generate", "批量生成 CDK", count=body.count, quota=body.quota, days=body.days)
    await session.commit()
    return {"created": len(created), "cards": created}


async def fuse_cards(session: AsyncSession, body: CardFuseInput, actor: str, user: User | None = None) -> dict[str, Any]:
    codes = body.codes
    hashes = [card_hash(code) for code in codes]
    # Adopt cards created by the legacy card store before validating a fusion.
    # This keeps the public fusion action consistent with the CDK check action.
    for code in codes:
        await resolve_card(session, code, for_update=True)
    rows = (await session.scalars(select(QuotaCard).where(QuotaCard.code_hash.in_(hashes)).order_by(QuotaCard.id).with_for_update())).all()
    by_hash = {card.code_hash: card for card in rows}
    if len(rows) != len(codes):
        raise HTTPException(status_code=400, detail="融合列表中存在无效 CDK")

    running = await session.scalar(select(Task.id).where(Task.owner_key.in_(hashes), Task.status.in_(["queued", "running", "waiting_blik", "waiting_plus"])).limit(1))
    if running:
        raise HTTPException(status_code=409, detail="来源 CDK 还有正在执行或排队的任务，请先取消任务再融合")

    sources: list[tuple[QuotaCard, int]] = []
    for code, hashed in zip(codes, hashes, strict=True):
        card = by_hash[hashed]
        if user:
            if not user.allow_fusion:
                raise HTTPException(status_code=403, detail="管理员已禁止当前用户融合 CDK")
            ensure_card_access(card, user)
        if card.status != "active" or is_expired(card.expires_at):
            raise HTTPException(status_code=400, detail=f"CDK {card.code_mask} 已停用或已融合")
        if card.fusion_disabled:
            raise HTTPException(status_code=400, detail=f"CDK {card.code_mask} 禁止融合")
        if card.reserved_quota > 0:
            raise HTTPException(status_code=409, detail=f"CDK {card.code_mask} 有任务额度正在预留，请先等待或取消任务")
        remain = card.quota - card.used_quota
        if remain <= 0:
            raise HTTPException(status_code=400, detail=f"CDK {card.code_mask} 剩余次数为 0")
        sources.append((card, remain))

    total_remaining = sum(remain for _, remain in sources)
    new_code = ""
    for _ in range(100):
        candidate = generate_card_code()
        exists = await session.scalar(select(QuotaCard.id).where(QuotaCard.code_hash == card_hash(candidate)))
        if not exists:
            new_code = candidate
            break
    if not new_code:
        raise HTTPException(status_code=503, detail="生成新 CDK 失败，请稍后重试")

    expires_at = min((card.expires_at for card, _ in sources if card.expires_at), default=None)
    new_card = QuotaCard(code_hash=card_hash(new_code), code_mask=mask_owner(new_code), code_value=new_code, quota=total_remaining, status="active", expires_at=expires_at, remark=body.remark.strip() or "CDK 融合", owner_user_id=user.id if user else None, bound_at=utcnow() if user else None)
    session.add(new_card)
    await session.flush()
    for card, _ in sources:
        card.status = "fused"
        card.version += 1
    fusion = CardFusion(
        id="FUS-" + uuid.uuid4().hex[:20].upper(),
        new_card_id=new_card.id,
        new_card_mask=new_card.code_mask,
        new_card_value=new_code,
        source_card_ids=[card.id for card, _ in sources],
        source_masks=[card.code_mask for card, _ in sources],
        source_values=codes,
        total_remaining=total_remaining,
        remark=body.remark.strip() or "CDK 融合",
        actor=actor,
    )
    session.add(fusion)
    await add_audit(session, "cards.fuse", "融合 CDK", fusionId=fusion.id, sourceMasks=fusion.source_masks, newMask=fusion.new_card_mask, quota=total_remaining, actor=actor)
    await session.commit()
    return {
        "ok": True,
        "card": {"code": new_code, "codeMask": new_card.code_mask, "quota": total_remaining, "usedQuota": 0, "remain": total_remaining, "status": "active"},
        "fusion": {"id": fusion.id, "sourceCodes": fusion.source_values, "newCode": fusion.new_card_value, "totalRemaining": total_remaining, "remark": fusion.remark, "createdAt": fusion.created_at.isoformat()},
    }


@app.post("/api/v1/cards/fuse", status_code=201)
async def public_fuse_cards(body: CardFuseInput, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if settings.execution_mode == "legacy":
        return await legacy_client.request("POST", "/api/card/fuse", json=body.model_dump())
    return await fuse_cards(session, body, "user", user)


@app.post("/api/v1/accounts/parse")
async def parse_accounts(body: AccountParse, _user: User = Depends(get_current_user)) -> dict[str, Any]:
    if settings.execution_mode == "legacy":
        return await legacy_client.parse_accounts(body.text)
    # A pasted session export is often pretty-printed over several lines.
    # Preserve the whole JSON object before falling back to one account per line.
    raw_text = body.text.strip()
    try:
        whole_session = json.loads(raw_text)
    except json.JSONDecodeError:
        whole_session = None
    lines = [raw_text] if isinstance(whole_session, dict) else [line.strip() for line in body.text.splitlines() if line.strip()]
    if len(lines) > 100:
        raise HTTPException(status_code=400, detail="单次最多解析 100 条")
    items = []
    for line in lines:
        normalized = line
        session_data: dict[str, Any] = {}
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    for container in (parsed.get("user"), parsed.get("account"), parsed.get("tokens"), parsed.get("credentials"), parsed.get("meta"), parsed):
                        if isinstance(container, dict):
                            session_data.update(container)
                    normalized = str(session_data.get("accessToken") or session_data.get("access_token") or session_data.get("token") or normalized).strip()
            except json.JSONDecodeError:
                pass
        header: dict[str, Any] = {}
        payload: dict[str, Any] = {}
        parts = normalized.split(".")
        if len(parts) == 3:
            try:
                header_padded = parts[0] + "=" * (-len(parts[0]) % 4)
                padded = parts[1] + "=" * (-len(parts[1]) % 4)
                header = json.loads(base64.urlsafe_b64decode(header_padded.encode()).decode("utf-8"))
                payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                header = {}
                payload = {}
        profile = payload.get("https://api.openai.com/profile") if isinstance(payload.get("https://api.openai.com/profile"), dict) else {}
        email = str(session_data.get("email") or profile.get("email") or payload.get("email") or payload.get("preferred_username") or "")
        exp = int(payload.get("exp") or 0) if str(payload.get("exp") or "").isdigit() else 0
        session_token = str(session_data.get("sessionToken") or session_data.get("session_token") or session_data.get("sessionCookie") or session_data.get("session_cookie") or session_data.get("__Secure-next-auth.session-token") or "").strip()
        items.append({"at": normalized, "sessionToken": session_token, "email": email, "atMask": f"{normalized[:12]}...{normalized[-6:]}", "type": "jwt" if len(parts) == 3 else "plain", "alg": header.get("alg", ""), "aud": payload.get("aud", ""), "subject": payload.get("sub", ""), "expired": bool(exp and exp < int(time.time())), "exp": exp})
    return {"count": len(items), "items": items}


def owner_header(value: str | None) -> str:
    if not value:
        raise HTTPException(status_code=400, detail="缺少 X-Tilian-CDK 请求头")
    return value.strip()


async def assert_user_card(session: AsyncSession, code: str, user: User) -> QuotaCard:
    card, imported = await resolve_card(session, code)
    if not card or card.status != "active":
        raise HTTPException(status_code=403, detail="CDK 不存在或已停用")
    ensure_card_access(card, user, bind=False)
    if imported:
        await session.commit()
    return card


@app.post("/api/v1/tasks", status_code=201)
async def create_task(
    body: TaskCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    request_id = idempotency_key or uuid.uuid4().hex
    if settings.execution_mode == "legacy":
        return await legacy_client.create_task(body.model_dump())
    try:
        return await TaskService(session).create(body, request_id, user)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/tasks")
async def list_tasks(
    x_tilian_cdk: str | None = Header(default=None, alias="X-Tilian-CDK"),
    limit: int = Query(default=30, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    cdk = owner_header(x_tilian_cdk)
    if settings.execution_mode == "legacy":
        return await legacy_client.list_tasks(cdk, limit)
    await assert_user_card(session, cdk, user)
    return await TaskService(session).list(cdk, limit)


@app.delete("/api/v1/tasks")
async def clear_tasks(
    x_tilian_cdk: str | None = Header(default=None, alias="X-Tilian-CDK"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cdk = owner_header(x_tilian_cdk)
    await assert_user_card(session, cdk, user)
    result = await session.execute(select(Task).where(Task.owner_key == owner_key(cdk)))
    tasks = result.scalars().all()
    for task in tasks:
        await TaskService(session).cancel(task.id, cdk)
        await session.delete(task)
    await session.commit()
    return {"cleared": len(tasks)}


@app.get("/api/v1/tasks/{task_id}")
async def get_task(
    task_id: str,
    x_tilian_cdk: str | None = Header(default=None, alias="X-Tilian-CDK"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cdk = owner_header(x_tilian_cdk)
    await assert_user_card(session, cdk, user)
    if settings.execution_mode == "legacy":
        return await legacy_client.get_task(task_id, cdk)
    result = await TaskService(session).get(task_id, cdk)
    if not result:
        raise HTTPException(status_code=404, detail="任务不存在")
    return result


@app.post("/api/v1/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    x_tilian_cdk: str | None = Header(default=None, alias="X-Tilian-CDK"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cdk = owner_header(x_tilian_cdk)
    await assert_user_card(session, cdk, user)
    if settings.execution_mode == "legacy":
        return await legacy_client.cancel_task(task_id, cdk)
    result = await TaskService(session).cancel(task_id, cdk)
    if not result:
        raise HTTPException(status_code=404, detail="任务不存在")
    return result


@app.post("/api/v1/tasks/{task_id}/blik/code")
async def submit_blik_code(task_id: str, body: BlikCodeInput, x_tilian_cdk: str | None = Header(default=None, alias="X-Tilian-CDK"), user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    cdk = owner_header(x_tilian_cdk)
    await assert_user_card(session, cdk, user)
    if settings.execution_mode == "legacy":
        return await legacy_client.request("POST", f"/api/tasks/{task_id}/blik/code", json={"itemIndex": body.itemIndex, "code": body.code}, headers={"X-Tilian-CDK": cdk})
    task = await session.scalar(select(Task).where(Task.id == task_id, Task.owner_key == owner_key(cdk)))
    if not task or body.itemIndex >= len(task.items):
        raise HTTPException(status_code=404, detail="任务或账号不存在")
    if task.mode != "blik":
        raise HTTPException(status_code=409, detail="当前任务不是 BLIK 通道")
    item = await session.scalar(select(TaskItem).where(TaskItem.task_id == task_id, TaskItem.item_index == body.itemIndex).with_for_update().execution_options(populate_existing=True))
    if not item:
        raise HTTPException(status_code=404, detail="账号不存在")
    if item.status != "waiting_blik":
        raise HTTPException(status_code=409, detail="当前账号不在等待 BLIK 动态码")
    queue_engine_control(session, task_id, item, {"type": "blik_code", "code": body.code})
    item.result = {**(item.result or {}), "blikSubmitted": True}
    item.stage = "blik_submitted"
    item.stage_detail = "动态码已入队，等待支付进程接收"
    item.status = "running"
    task.status = "running"
    await session.commit()
    return (await TaskService(session).get(task_id, cdk)) or {}


@app.post("/api/v1/tasks/{task_id}/qr/refresh")
async def refresh_qr(task_id: str, body: QrRefreshInput, x_tilian_cdk: str | None = Header(default=None, alias="X-Tilian-CDK"), user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    cdk = owner_header(x_tilian_cdk)
    await assert_user_card(session, cdk, user)
    if settings.execution_mode == "legacy":
        return await legacy_client.request("POST", f"/api/tasks/{task_id}/qr/refresh", json=body.model_dump(), headers={"X-Tilian-CDK": cdk})
    task = await session.scalar(select(Task).where(Task.id == task_id, Task.owner_key == owner_key(cdk)))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    item = await session.scalar(select(TaskItem).where(TaskItem.task_id == task_id, TaskItem.item_index == body.itemIndex).with_for_update().execution_options(populate_existing=True))
    if not item:
        raise HTTPException(status_code=404, detail="账号不存在")
    extra = item.result or {}
    if item.status not in {"running", "waiting_plus"} or not (extra.get("qrRefreshActive") or extra.get("qr_refresh_active")):
        raise HTTPException(status_code=409, detail="二维码会话已结束或尚未就绪，请重新提链")
    queue_engine_control(session, task_id, item, {"command": "refresh_qr"})
    item.result = {**(item.result or {}), "qrRefreshRequestedAt": int(time.time() * 1000), "qrPending": True}
    item.stage_detail = "已请求刷新二维码"
    await session.commit()
    return (await TaskService(session).get(task_id, cdk)) or {}


@app.get("/api/v1/tasks/{task_id}/events")
async def task_events(
    task_id: str,
    x_tilian_cdk: str | None = Header(default=None, alias="X-Tilian-CDK"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    after_event_id: Annotated[str | None, Query(alias="afterEventId", max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> StreamingResponse:
    cdk = owner_header(x_tilian_cdk)
    await assert_user_card(session, cdk, user)
    task = await session.scalar(select(Task).where(Task.id == task_id, Task.owner_key == owner_key(cdk)))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    query = select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.event_type != CONTROL_EVENT)
    if after_event_id is not None:
        cursor = await session.scalar(query.where(TaskEvent.event_id == after_event_id))
        if cursor is None:
            raise HTTPException(status_code=400, detail="事件游标无效，请重新加载任务日志")
        query = query.where(or_(
            TaskEvent.created_at > cursor.created_at,
            and_(TaskEvent.created_at == cursor.created_at, TaskEvent.event_id > cursor.event_id),
        ))
    events = (await session.scalars(query.order_by(TaskEvent.created_at.asc(), TaskEvent.event_id.asc()).limit(limit))).all()

    async def stream():
        for event in events:
            payload = {"eventId": event.event_id, "type": event.event_type, "itemIndex": event.item_index, "payload": event.payload, "createdAt": event.created_at.isoformat()}
            import json as _json
            yield f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/admin/cluster")
async def cluster_status(authorization: str | None = Header(default=None, alias="Authorization")) -> dict[str, Any]:
    require_admin(authorization)
    return {
        "mode": settings.execution_mode,
        "database": settings.database_url.split(":", 1)[0],
        "redisConfigured": settings.redis_enabled,
        "kafkaConfigured": settings.kafka_enabled,
        "consumerGroup": settings.kafka_consumer_group,
        "topics": {"commands": settings.kafka_command_topic, "events": settings.kafka_event_topic, "dlq": settings.kafka_dlq_topic},
    }


def require_admin(value: str | None) -> None:
    expected = settings.admin_api_token.strip()
    supplied = value.removeprefix("Bearer ").strip() if value and value.startswith("Bearer ") else ""
    if not expected or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="管理员令牌无效")


@app.get("/api/v1/admin/overview")
async def admin_overview(
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_admin(authorization)

    async def count_tasks(*statuses: str) -> int:
        query = select(func.count()).select_from(Task)
        if statuses:
            query = query.where(Task.status.in_(statuses))
        return int(await session.scalar(query) or 0)

    async def count_items(*statuses: str) -> int:
        query = select(func.count()).select_from(TaskItem)
        if statuses:
            query = query.where(TaskItem.status.in_(statuses))
        return int(await session.scalar(query) or 0)

    task_total = await count_tasks()
    queued = await count_tasks("queued")
    running = await count_tasks("running", "waiting_blik", "waiting_plus")
    task_success = await count_tasks("success")
    task_failed = await count_tasks("failed", "timed_out")
    item_total = await count_items()
    item_success = await count_items("success", "plus_success")
    item_failed = await count_items("failed", "timed_out")
    card_total = int(await session.scalar(select(func.count()).select_from(QuotaCard)) or 0)
    active_cards = int(await session.scalar(select(func.count()).select_from(QuotaCard).where(QuotaCard.status == "active")) or 0)
    quota, used, reserved = (
        await session.execute(
            select(
                func.coalesce(func.sum(QuotaCard.quota), 0),
                func.coalesce(func.sum(QuotaCard.used_quota), 0),
                func.coalesce(func.sum(QuotaCard.reserved_quota), 0),
            )
        )
    ).one()
    return {
        "tasks": {"total": task_total, "queued": queued, "running": running, "success": task_success, "failed": task_failed},
        "items": {"total": item_total, "success": item_success, "failed": item_failed},
        "cards": {"total": card_total, "active": active_cards, "quota": int(quota), "used": int(used), "reserved": int(reserved)},
    }


@app.get("/api/v1/admin/cards")
async def admin_cards(
    authorization: str | None = Header(default=None, alias="Authorization"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_admin(authorization)
    total = int(await session.scalar(select(func.count()).select_from(QuotaCard)) or 0)
    result = await session.scalars(select(QuotaCard).order_by(QuotaCard.created_at.desc()).offset(offset).limit(limit))
    cards_rows = result.all()
    owner_ids = {card.owner_user_id for card in cards_rows if card.owner_user_id is not None}
    owner_names = {}
    if owner_ids:
        owner_names = {user.id: user.username for user in (await session.scalars(select(User).where(User.id.in_(owner_ids)))).all()}
    items = [
        {
            "id": card.id,
            "codeMask": card.code_mask,
            "code": card.code_value or card.code_mask,
            "quota": card.quota,
            "usedQuota": card.used_quota,
            "reservedQuota": card.reserved_quota,
            "remain": max(0, card.quota - card.used_quota - card.reserved_quota),
            "status": card.status,
            "fusionDisabled": card.fusion_disabled,
            "ownerUserId": card.owner_user_id,
            "ownerUsername": owner_names.get(card.owner_user_id) if card.owner_user_id is not None else None,
            "boundAt": card.bound_at.isoformat() if card.bound_at else None,
            "remark": card.remark or "",
            "createdAt": card.created_at.isoformat(),
            "updatedAt": card.updated_at.isoformat(),
        }
        for card in cards_rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/admin/users")
async def admin_users(
    authorization: str | None = Header(default=None, alias="Authorization"),
    q: str = Query(default="", max_length=120),
    status: str = Query(default="all", pattern="^(all|active|disabled)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_admin(authorization)
    query = select(User).order_by(User.created_at.desc())
    count_query = select(func.count()).select_from(User)
    if status != "all":
        query = query.where(User.status == status)
        count_query = count_query.where(User.status == status)
    if q.strip():
        needle = f"%{q.strip().lower()}%"
        condition = (User.username.ilike(needle) | User.display_name.ilike(needle) | User.notes.ilike(needle))
        query = query.where(condition)
        count_query = count_query.where(condition)
    total = int(await session.scalar(count_query) or 0)
    users = []
    concurrency = effective_concurrency(await load_concurrency_config(session))
    for user in (await session.scalars(query.offset(offset).limit(limit))).all():
        tasks_count = int(await session.scalar(select(func.count()).select_from(Task).where(Task.user_id == user.id)) or 0)
        cards_count = int(await session.scalar(select(func.count()).select_from(QuotaCard).where(QuotaCard.owner_user_id == user.id)) or 0)
        users.append({**user_public(user, concurrency=concurrency), "taskCount": tasks_count, "cardCount": cards_count, "online": bool(user.last_login_at and (datetime.now(timezone.utc) - user.last_login_at).total_seconds() < 900)})
    return {"items": users, "total": total, "limit": limit, "offset": offset}


@app.patch("/api/v1/admin/users/{user_id}")
async def update_admin_user(user_id: int, body: AdminUserUpdate, authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if body.taskConcurrency is not None:
        raise HTTPException(status_code=400, detail="普通用户并发统一管理，请在并发配置中修改")
    values = body.model_dump(exclude_none=True, by_alias=True)
    mapping = {"displayName": "display_name", "taskConcurrency": "task_concurrency", "maxAccountsPerTask": "max_accounts_per_task", "allowFusion": "allow_fusion", "notes": "notes", "status": "status"}
    for key, value in values.items():
        setattr(user, mapping[key], value)
    user.token_version += 1 if "status" in values and values["status"] == "disabled" else 0
    await add_audit(session, "user.update", "更新用户配置", userId=user.id, changes=values)
    await session.commit()
    concurrency = effective_concurrency(await load_concurrency_config(session))
    return {"ok": True, "user": user_public(user, concurrency=concurrency)}


@app.get("/api/v1/admin/user-policy")
async def admin_user_policy(authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    config = await session.get(RuntimeConfig, "user_policy")
    values = UserPolicyConfig(**(config.value if config else {})).model_dump(by_alias=True)
    values["defaultConcurrency"] = (await load_concurrency_config(session))["default"]
    return values


@app.put("/api/v1/admin/user-policy")
async def update_admin_user_policy(body: UserPolicyConfig, authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    values = body.model_dump(by_alias=True)
    # Older clients still send their cached registration default. The shared
    # concurrency page is the only writer of this setting.
    values["defaultConcurrency"] = (await load_concurrency_config(session))["default"]
    config = await session.get(RuntimeConfig, "user_policy")
    if config:
        config.value = values
    else:
        session.add(RuntimeConfig(key="user_policy", value=values))
    await add_audit(session, "user_policy.update", "更新用户动态策略", **values)
    await session.commit()
    return values


@app.get("/api/v1/admin/tasks")
async def admin_tasks(
    authorization: str | None = Header(default=None, alias="Authorization"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_admin(authorization)
    total = int(await session.scalar(select(func.count()).select_from(Task)) or 0)
    result = await session.scalars(select(Task).order_by(Task.created_at.desc()).offset(offset).limit(limit))
    tasks_rows = result.unique().all()
    owner_keys = [task.owner_key for task in tasks_rows]
    card_codes = {}
    if owner_keys:
        card_codes = {code_hash_value: code_value for code_hash_value, code_value in (await session.execute(select(QuotaCard.code_hash, QuotaCard.code_value).where(QuotaCard.code_hash.in_(owner_keys)))).all() if code_value}
    items = []
    for task in tasks_rows:
        items.append(
            {
                "id": task.id,
                "ownerMask": task.owner_mask,
                "cardCode": task.owner_value or card_codes.get(task.owner_key, ""),
                "mode": task.mode,
                "modeName": task.mode_name,
                "status": task.status,
                "concurrency": task.concurrency,
                "itemCount": len(task.items),
                "successCount": sum(item.status in {"success", "plus_success"} for item in task.items),
                "failedCount": sum(item.status in {"failed", "timed_out"} for item in task.items),
                "createdAt": task.created_at.isoformat(),
                "updatedAt": task.updated_at.isoformat(),
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.post("/api/v1/admin/cards/import", status_code=201)
async def import_cards(
    body: CardImport,
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_admin(authorization)
    created = 0
    updated = 0
    for item in body.cards:
        normalized = item.code.strip().upper()
        hashed = card_hash(normalized)
        card = await session.scalar(select(QuotaCard).where(QuotaCard.code_hash == hashed))
        if card:
            card.quota = item.quota
            card.status = "active"
            card.version += 1
            updated += 1
        else:
            session.add(QuotaCard(code_hash=hashed, code_mask=mask_owner(normalized), code_value=normalized, quota=item.quota))
            created += 1
    await session.commit()
    return {"ok": True, "created": created, "updated": updated}


@app.post("/api/v1/admin/cards/fuse", status_code=201)
async def admin_fuse_cards(body: CardFuseInput, authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    return await fuse_cards(session, body, "admin")


@app.get("/api/v1/admin/card-fusions")
async def admin_card_fusions(authorization: str | None = Header(default=None, alias="Authorization"), limit: int = Query(default=100, ge=1, le=500), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    rows = (await session.scalars(select(CardFusion).order_by(CardFusion.created_at.desc()).limit(limit))).all()
    return {"total": len(rows), "records": [{"id": row.id, "newCode": row.new_card_value or row.new_card_mask, "sourceCodes": row.source_values or row.source_masks, "totalRemaining": row.total_remaining, "remark": row.remark, "actor": row.actor, "createdAt": row.created_at.isoformat()} for row in rows]}


async def add_audit(session: AsyncSession, action: str, message: str, **payload: Any) -> None:
    session.add(AuditLog(action=action, message=message, payload=payload))


@app.get("/api/v1/announcement")
async def public_announcement(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    announcement = await session.scalar(select(AdminAnnouncement).where(AdminAnnouncement.status == "published").order_by(AdminAnnouncement.updated_at.desc()))
    return {"announcement": {"title": announcement.title, "content": announcement.content, "updatedAt": announcement.updated_at.isoformat()} if announcement else None}


@app.get("/api/v1/admin/concurrency")
async def admin_concurrency(authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    return await load_concurrency_config(session)


@app.put("/api/v1/admin/concurrency")
async def update_concurrency(body: ConcurrencyConfig, authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    config = await session.get(RuntimeConfig, "concurrency")
    values = body.model_dump(by_alias=True)
    if config:
        config.value = values
    else:
        session.add(RuntimeConfig(key="concurrency", value=values))
    await add_audit(session, "concurrency.update", "更新并发配置", **values)
    await session.commit()
    return values


@app.get("/api/v1/admin/proxies")
async def admin_proxies(authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    config = await session.get(RuntimeConfig, "proxy_pools")
    channels = config.value.get("channels", {}) if config and isinstance(config.value, dict) else {}
    return {"channels": {mode: {"enabled": False, "primaryText": "", "exitText": "", **(channels.get(mode, {}) if isinstance(channels.get(mode, {}), dict) else {})} for mode in sorted(SUPPORTED_MODES)}}


@app.put("/api/v1/admin/proxies")
async def update_admin_proxies(body: ProxyPoolsConfig, authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    values = body.model_dump()
    for mode, channel in values["channels"].items():
        try:
            channel["primaryText"] = format_proxy_pool(normalize_proxy_pool(channel["primaryText"], f"{mode} 建单代理池"))
            channel["exitText"] = format_proxy_pool(normalize_proxy_pool(channel["exitText"], f"{mode} 出口代理池"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if channel["enabled"] and not channel["primaryText"]:
            raise HTTPException(status_code=422, detail=f"{mode} 已启用但建单代理池为空")
    config = await session.get(RuntimeConfig, "proxy_pools")
    if config:
        config.value = values
    else:
        session.add(RuntimeConfig(key="proxy_pools", value=values))
    counts = {mode: {"primary": len(channel["primaryText"].splitlines()), "exit": len(channel["exitText"].splitlines())} for mode, channel in values["channels"].items()}
    await add_audit(session, "proxy_pools.update", "更新通道代理池", counts=counts)
    await session.commit()
    return values


@app.get("/api/v1/admin/announcement")
async def admin_announcement(authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    announcement = await session.scalar(select(AdminAnnouncement).where(AdminAnnouncement.status == "published").order_by(AdminAnnouncement.updated_at.desc()))
    return {"announcement": {"id": announcement.id, "title": announcement.title, "content": announcement.content, "updatedAt": announcement.updated_at.isoformat()} if announcement else None}


@app.post("/api/v1/admin/announcement")
async def publish_announcement(body: AnnouncementInput, authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    previous = (await session.scalars(select(AdminAnnouncement).where(AdminAnnouncement.status == "published"))).all()
    for item in previous:
        item.status = "archived"
    announcement = AdminAnnouncement(title=body.title.strip() or "系统公告", content=body.content.strip(), status="published")
    session.add(announcement)
    await add_audit(session, "announcement.publish", "发布系统公告", title=announcement.title)
    await session.commit()
    return {"ok": True, "announcement": {"id": announcement.id, "title": announcement.title, "content": announcement.content, "updatedAt": announcement.updated_at.isoformat()}}


@app.delete("/api/v1/admin/announcement")
async def remove_announcement(authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    result = await session.execute(select(AdminAnnouncement).where(AdminAnnouncement.status == "published"))
    count = 0
    for item in result.scalars().all():
        item.status = "archived"
        count += 1
    await add_audit(session, "announcement.remove", "撤下系统公告", count=count)
    await session.commit()
    return {"ok": True, "deleted": count}


@app.get("/api/v1/admin/logs")
async def admin_logs(authorization: str | None = Header(default=None, alias="Authorization"), limit: int = Query(default=100, ge=1, le=500), level: str | None = None, mode: str | None = None, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if level:
        query = query.where(AuditLog.level == level)
    if mode:
        query = query.where(AuditLog.mode == mode)
    rows = (await session.scalars(query)).all()
    return {"total": len(rows), "logs": [{"id": row.id, "level": row.level, "action": row.action, "message": row.message, "mode": row.mode, "taskId": row.task_id, "payload": row.payload, "createdAt": row.created_at.isoformat()} for row in rows]}


@app.post("/api/v1/admin/logs/clear")
async def clear_admin_logs(authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    result = await session.execute(delete(AuditLog))
    await session.commit()
    return {"ok": True, "deleted": result.rowcount or 0}


@app.get("/api/v1/admin/route-stats")
async def admin_route_stats(authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    rows = (await session.execute(select(Task.mode, Task.status, func.count()).group_by(Task.mode, Task.status))).all()
    grouped: dict[str, dict[str, int]] = {}
    for mode, status, count in rows:
        grouped.setdefault(mode, {})[status] = int(count)
    return {"items": [{"mode": mode, "total": sum(statuses.values()), "success": statuses.get("success", 0) + statuses.get("plus_success", 0), "failed": statuses.get("failed", 0) + statuses.get("timed_out", 0), "running": statuses.get("running", 0) + statuses.get("waiting_blik", 0) + statuses.get("waiting_plus", 0), "statuses": statuses} for mode, statuses in grouped.items()]}


@app.post("/api/v1/admin/danger/reset-stats")
async def reset_admin_stats(authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    await session.execute(delete(TaskEvent))
    await session.execute(delete(AuditLog))
    await session.commit()
    return {"ok": True}


@app.patch("/api/v1/admin/cards/{card_id}")
async def update_card_status(card_id: int, body: AdminCardStatusInput, authorization: str | None = Header(default=None, alias="Authorization"), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_admin(authorization)
    card = await session.get(QuotaCard, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="卡密不存在")
    if body.status is None and body.fusionDisabled is None:
        raise HTTPException(status_code=400, detail="至少提供状态或禁融设置")
    if body.status is not None:
        card.status = body.status
    if body.fusionDisabled is not None:
        card.fusion_disabled = body.fusionDisabled
    await add_audit(session, "card.update", "更新卡密状态或融合设置", cardId=card_id, status=card.status, fusionDisabled=card.fusion_disabled)
    await session.commit()
    return {"ok": True, "id": card.id, "status": card.status, "fusionDisabled": card.fusion_disabled}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.app_env == "dev")
