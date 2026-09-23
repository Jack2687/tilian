import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.engine_adapter import EngineAdapter
from app.engine_protocol import CONTROL_EVENT, apply_engine_event
from app.models import Task, TaskEvent, TaskItem, User
from app.schemas import BlikCodeInput, QrRefreshInput
from app.security import encrypt_credential, owner_key
from app.services import engine_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,result", [
    ("gcash", {"ok": True, "link": "https://payments.example/qr", "extra": {"qr_image": "data:image/png;base64,qr", "qr_refresh_active": True}}),
    ("ideal", {"ok": True, "link": "https://payments.example/qr", "checkout_amount": 0, "promo_applied": True}),
    ("upi", {"ok": True, "link": "https://payments.stripe.com/upi/instructions/fixture", "checkout_amount": 0, "promo_applied": True}),
    ("pix", {"ok": True, "link": "https://payments.example/qr", "qr_image_url": "https://qr.example/image.png"}),
])
async def test_final_result_does_not_wait_for_qr_watcher(monkeypatch, mode, result):
    adapter = EngineAdapter(timeout_seconds=2)
    source = "import json,time\nprint('[gateway] [ok] ready', flush=True)\nprint(" + repr(json.dumps(result)) + ", flush=True)\ntime.sleep(30)"
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", source])
    messages = []

    async def on_event(message):
        messages.append(message)

    started = time.monotonic()
    actual, stages = await adapter.execute(mode, "test", "", "", {}, on_event=on_event)
    assert actual["ok"] is True, actual
    assert time.monotonic() - started < 2
    assert stages[-1]["stage"] == "gateway"
    assert messages[-1]["event"] == "stage"
    assert actual["extra"]["qrRefreshActive"] is False
    assert actual["extra"]["qrPending"] is False


@pytest.mark.asyncio
async def test_blik_waiting_event_delivers_code_to_same_process(monkeypatch):
    adapter = EngineAdapter(timeout_seconds=3)
    source = """import json,sys
print(json.dumps({'event':'blik_waiting','attempt':1,'max_attempts':3}), flush=True)
message = json.loads(sys.stdin.readline())
assert message == {'type':'blik_code','code':'123456'}
print(json.dumps({'event':'blik_processing','attempt':1}), flush=True)
print(json.dumps({'ok':True,'paid':True,'payment_status':'plus_active','link':'https://checkout.example/paid'}), flush=True)
"""
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", source])
    controls = asyncio.Queue()
    states = []
    item = TaskItem(status="running", result={})

    async def on_event(message):
        apply_engine_event(item, message)
        states.append(item.status)
        if message.get("event") == "blik_waiting":
            controls.put_nowait({"type": "blik_code", "code": "123456"})

    async def read_control():
        return controls.get_nowait() if not controls.empty() else None

    actual, _ = await adapter.execute("blik", "test", "", "", {}, on_event=on_event, read_control=read_control)
    assert actual["ok"] is True
    assert states == ["waiting_blik", "running"]
    assert actual["extra"]["paymentStatus"] == "plus_active"


@pytest.mark.asyncio
async def test_event_with_ok_is_not_mistaken_for_final_result(monkeypatch):
    adapter = EngineAdapter(timeout_seconds=2)
    source = "import json\nprint(json.dumps({'event':'qr_refresh','ok':True,'qr_image':'https://qr.example/a.png'}), flush=True)\nprint(json.dumps({'ok':False,'error':'checkout rejected','retryable':False}), flush=True)"
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", source])
    actual, _ = await adapter.execute("pix", "test", "", "", {})
    assert actual["ok"] is False
    assert actual["error"] == "checkout rejected"


@pytest.mark.asyncio
async def test_pending_gcash_result_waits_for_first_qr_before_stopping(monkeypatch):
    adapter = EngineAdapter(timeout_seconds=3)
    source = """import json,time
print(json.dumps({'ok':True,'link':'https://payments.example/qr','extra':{'qr_pending':True,'qr_image':'','qr_refresh_active':True}}), flush=True)
time.sleep(.2)
print(json.dumps({'event':'qr_refresh','qr_image':'data:image/png;base64,first-qr','qr_refresh_active':True}), flush=True)
time.sleep(30)
"""
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", source])
    messages = []

    async def on_event(message):
        messages.append(message)

    actual, _ = await adapter.execute("gcash", "test", "", "", {}, on_event=on_event)
    assert actual["ok"] is True, actual
    assert actual["extra"]["qrImage"] == "data:image/png;base64,first-qr"
    assert actual["extra"]["qrPending"] is False
    assert [message["event"] for message in messages] == ["result_pending", "qr_refresh"]


@pytest.mark.asyncio
async def test_pending_gcash_exit_without_qr_is_not_success(monkeypatch):
    adapter = EngineAdapter(timeout_seconds=2)
    source = "import json\nprint(json.dumps({'ok':True,'link':'https://payments.example/qr','extra':{'qr_pending':True,'qr_image':''}}), flush=True)"
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", source])
    actual, _ = await adapter.execute("gcash", "test", "", "", {})
    assert actual["ok"] is False
    assert actual["reason_code"] == "engine_qr_incomplete"


@pytest.mark.asyncio
async def test_qr_event_before_pending_result_is_retained(monkeypatch):
    adapter = EngineAdapter(timeout_seconds=2)
    source = """import json,time
print(json.dumps({'event':'qr_refresh','qr_image':'https://qr.example/first.png'}), flush=True)
print(json.dumps({'ok':True,'link':'https://payments.example/qr','extra':{'qr_pending':True,'qr_image':''}}), flush=True)
time.sleep(30)
"""
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", source])
    actual, _ = await adapter.execute("gcash", "test", "", "", {})
    assert actual["ok"] is True
    assert actual["extra"]["qrImage"] == "https://qr.example/first.png"


@pytest.mark.asyncio
async def test_large_qr_json_line_is_supported(monkeypatch):
    adapter = EngineAdapter(timeout_seconds=2)
    source = "import json\nprint(json.dumps({'ok':True,'link':'https://pix.example/qr','qr_image':'data:image/png;base64,'+'a'*100000}), flush=True)"
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", source])
    actual, _ = await adapter.execute("pix", "test", "", "", {})
    assert actual["ok"] is True
    assert len(actual["extra"]["qrImage"]) > 100000


@pytest.mark.asyncio
async def test_canceled_waiting_process_is_stopped(monkeypatch):
    adapter = EngineAdapter(timeout_seconds=3)
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", "import time; time.sleep(30)"])

    async def read_control():
        return {"command": "cancel"}

    actual, _ = await adapter.execute("blik", "test", "", "", {}, read_control=read_control)
    assert actual["reason_code"] == "engine_canceled"
    assert actual["retryable"] is False


@pytest.mark.asyncio
async def test_timeout_cleans_process_without_result(monkeypatch):
    adapter = EngineAdapter(timeout_seconds=0.1)
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", "import time; time.sleep(30)"])
    actual, _ = await adapter.execute("pix", "test", "", "", {})
    assert actual["reason_code"] == "engine_timeout"


@pytest.mark.asyncio
async def test_blik_api_enqueues_encrypted_control_and_worker_consumes_once(monkeypatch):
    from app import main

    db = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(engine_sessions, "SessionLocal", sessions)
    monkeypatch.setattr(main.settings, "execution_mode", "distributed")

    async def assert_card(*_):
        return None

    monkeypatch.setattr(main, "assert_user_card", assert_card)
    try:
        async with sessions() as session:
            task = Task(id="blik-live-test", owner_key=owner_key("test-card"), owner_mask="test", mode="blik", mode_name="BLIK", status="running", request_id="blik-live-test", payload={})
            task.items.append(TaskItem(item_index=0, credential=encrypt_credential("test-at"), status="running", attempt=1, result={}))
            session.add(task)
            await session.commit()
        await engine_sessions.persist_engine_event("blik-live-test", 0, 1, {"event": "blik_waiting", "attempt": 1})
        async with sessions() as session:
            result = await main.submit_blik_code("blik-live-test", BlikCodeInput(itemIndex=0, code="123456"), "test-card", User(id=1), session)
            assert result["items"][0]["status"] == "running"
            event = await session.scalar(select(TaskEvent).where(TaskEvent.event_type == CONTROL_EVENT))
            assert event and "123456" not in json.dumps(event.payload)
            response = await main.task_events("blik-live-test", "test-card", User(id=1), session)
            public_events = "".join([chunk async for chunk in response.body_iterator])
            assert CONTROL_EVENT not in public_events
            assert "encryptedCode" not in public_events
            with pytest.raises(HTTPException) as error:
                await main.submit_blik_code("blik-live-test", BlikCodeInput(itemIndex=0, code="654321"), "test-card", User(id=1), session)
            assert error.value.status_code == 409
        control = await engine_sessions.pop_engine_control("blik-live-test", 0, 1)
        assert control == {"type": "blik_code", "code": "123456"}
        assert await engine_sessions.pop_engine_control("blik-live-test", 0, 1) is None
        async with sessions() as session:
            item = await session.scalar(select(TaskItem))
            item.status = "success"
            item.result = {"qrRefreshActive": False}
            await session.commit()
            with pytest.raises(HTTPException) as error:
                await main.refresh_qr("blik-live-test", QrRefreshInput(itemIndex=0), "test-card", User(id=1), session)
            assert error.value.status_code == 409
            item.status = "running"
            item.result = {"qrRefreshActive": True, "qrPending": True}
            await session.commit()
            await main.refresh_qr("blik-live-test", QrRefreshInput(itemIndex=0), "test-card", User(id=1), session)
        assert await engine_sessions.pop_engine_control("blik-live-test", 0, 1) == {"command": "refresh_qr"}
    finally:
        await db.dispose()


def test_upi_payment_stage_keeps_one_sticky_proxy():
    command = EngineAdapter().command("upi", "test", "", "http://primary:80", {"proxiesText": "http://primary:80", "exitProxiesText": "socks5://exit-one:1080\nsocks5://exit-two:1080"}, "socks5://exit-one:1080")
    assert "--proxy-pool" not in command
    assert command[command.index("--proxy") + 1] == "http://primary:80"
    assert command[command.index("--promotion-proxy") + 1] == "http://primary:80"


def test_worker_upi_uses_reference_firefox_fingerprint(monkeypatch):
    from app import worker

    calls = []

    def generate(country, *, channel="", kernel="", impersonate="", platform="", locale=""):
        calls.append((country, channel, kernel, impersonate, platform, locale))
        return {"kernel": kernel, "impersonate": impersonate, "platform_os": platform, "locale": locale}

    monkeypatch.setattr(worker.fingerprints, "generate", generate)
    result = worker.build_attempt_fingerprint("upi", "IN", "user@example.com", "at", 1)
    assert result["kernel"] == "chrome"
    assert result["impersonate"] == "chrome146-macos"
    # The rail's identity pins en-IN; the IN locale pool also contains hi-IN.
    assert result["locale"] == "en-IN"
    assert calls == [("IN", "upi", "chrome", "chrome146-macos", "macOS", "en-IN")]
    second = worker.build_attempt_fingerprint("upi", "IN", "user@example.com", "at", 2)
    assert second["impersonate"] == "chrome150-macos"
    third = worker.build_attempt_fingerprint("upi", "IN", "user@example.com", "at", 3)
    assert third["kernel"] == "firefox"
    assert third["impersonate"] == "firefox147-macos"


@pytest.mark.asyncio
async def test_worker_blik_api_to_subprocess_to_quota_settlement(monkeypatch):
    from app import main, worker
    from app.models import QuotaCard

    db = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(engine_sessions, "SessionLocal", sessions)
    monkeypatch.setattr(main.settings, "execution_mode", "distributed")
    monkeypatch.setattr(worker.fingerprints, "get_or_create", lambda **_: {})

    @asynccontextmanager
    async def lease(*_):
        yield True

    async def no_network(*_):
        return None

    monkeypatch.setattr(worker.cache, "lease", lease)
    monkeypatch.setattr(worker, "publish_event", no_network)
    monkeypatch.setattr(main, "assert_user_card", no_network)
    adapter = EngineAdapter(timeout_seconds=3)
    source = """import json,sys
print(json.dumps({'event':'blik_waiting','attempt':1}), flush=True)
code=json.loads(sys.stdin.readline())['code']
assert code == '123456'
print(json.dumps({'ok':True,'paid':True,'payment_status':'plus_active','link':'https://checkout.example/paid'}), flush=True)
"""
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", source])
    monkeypatch.setattr(worker, "adapter", adapter)
    running = None
    try:
        async with sessions() as session:
            task = Task(id="worker-blik", owner_key=owner_key("test-card"), owner_mask="test", mode="blik", mode_name="BLIK", status="queued", request_id="worker-blik", payload={})
            task.items.append(TaskItem(item_index=0, credential=encrypt_credential("test-at"), status="queued", result={}))
            session.add(task)
            session.add(QuotaCard(code_hash=owner_key("test-card"), code_mask="test", quota=3, used_quota=0, reserved_quota=1))
            await session.commit()
        running = asyncio.create_task(worker.execute_item("worker-blik", 0, asyncio.Semaphore(1)))
        for _ in range(100):
            async with sessions() as session:
                item = await session.scalar(select(TaskItem))
                if item.status == "waiting_blik":
                    break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("Worker did not publish BLIK waiting state")
        async with sessions() as session:
            await main.submit_blik_code("worker-blik", BlikCodeInput(itemIndex=0, code="123456"), "test-card", User(id=1), session)
        await asyncio.wait_for(running, 3)
        async with sessions() as session:
            item = await session.scalar(select(TaskItem))
            card = await session.scalar(select(QuotaCard))
            assert item.status == "success", item.error
            assert item.link == "https://checkout.example/paid"
            assert item.result["paymentStatus"] == "plus_active"
            assert card.used_quota == 1
            assert card.reserved_quota == 0
    finally:
        if running and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await db.dispose()
