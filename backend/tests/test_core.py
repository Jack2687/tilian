from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, select

from app.database import SessionLocal, create_schema
from app.card_sync import resolve_card
from app.engine_adapter import EngineAdapter
from app.main import app, settings
from app.models import OutboxEvent, QuotaCard, QuotaLedger, Task
from app.proxy import normalize_proxy_pool, normalize_proxy_url
from app.schemas import AccountInput, TaskCreate
from app.security import decrypt_credential, encrypt_credential, mask_owner, owner_key
from app.services.tasks import TaskService


def test_health_live() -> None:
    with TestClient(app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_credentials_are_encrypted_at_rest() -> None:
    raw = "eyJ.example.secret"
    encrypted = encrypt_credential(raw)
    assert raw.encode() not in encrypted
    assert decrypt_credential(encrypted) == raw


def test_owner_identity_is_hashed_and_masked() -> None:
    code = "TL-ABCD-EFGH-IJKL"
    assert owner_key(code) == owner_key(code.lower())
    assert code not in owner_key(code)
    assert mask_owner(code).startswith("TL-")
    assert mask_owner(code).endswith("JKL")


def test_engine_adapter_builds_supported_commands() -> None:
    adapter = EngineAdapter()
    payload = {
        "params": {"forceMethod": True, "forceZero": True},
        "proxiesText": "http://primary",
        "exitProxiesText": "http://exit",
    }
    for mode in ("paypal", "gopay", "gcash", "momo", "upi", "blik", "pix", "ideal", "card"):
        command = adapter.command(mode, "token", "user@example.com", "http://primary", payload, "http://exit")
        assert command
        assert "token" in command
    momo = adapter.command("momo", "token", "user@example.com", "http://primary", payload, "http://exit")
    assert momo[momo.index("--exit-proxy") + 1] == "http://exit"
    upi = adapter.command("upi", "token", "user@example.com", "http://primary", payload, "http://exit")
    assert upi[upi.index("--proxy") + 1] == "http://primary"
    assert upi[upi.index("--promotion-proxy") + 1] == "http://primary"



def test_proxy_normalization_accepts_1024proxy_export_format() -> None:
    raw = "proxy.example.test:3000:demo-user-region-IN-sid-ABC123-t-5:demo-password"
    expected = "http://demo-user-region-IN-sid-ABC123-t-5:demo-password@proxy.example.test:3000"
    assert normalize_proxy_url(raw) == expected
    assert normalize_proxy_url("user:pass@proxy.example:8080") == "http://user:pass@proxy.example:8080"
    assert normalize_proxy_url("socks5://user:pass@proxy.example:1080") == "socks5://user:pass@proxy.example:1080"
    assert normalize_proxy_pool(f"{raw}\n{raw}") == [expected]


def test_admin_endpoints_require_token_and_expose_manageable_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_token", "test-admin-secret")
    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/admin/overview")
        authorized = client.get(
            "/api/v1/admin/cards",
            headers={"Authorization": "Bearer test-admin-secret"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    body = authorized.json()
    assert "items" in body
    for card in body["items"]:
        assert "codeMask" in card
        assert "code" in card
        assert "codeHash" not in card and "code_hash" not in card


def test_admin_proxy_pools_are_private_and_report_public_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_token", "test-admin-secret")
    headers = {"Authorization": "Bearer test-admin-secret"}
    value = {
        "channels": {
            "paypal": {
                "enabled": True,
                "primaryText": "http://primary-one\nhttp://primary-two",
                "exitText": "http://exit-one",
            }
        }
    }
    with TestClient(app) as client:
        updated = client.put("/api/v1/admin/proxies", headers=headers, json=value)
        private = client.get("/api/v1/admin/proxies", headers=headers)
        public = client.get("/api/v1/meta")

    assert updated.status_code == 200
    assert private.json()["channels"]["paypal"]["primaryText"] == value["channels"]["paypal"]["primaryText"]
    public_text = public.text
    assert "primary-one" not in public_text
    assert public.json()["engine"]["proxyPools"]["paypal"] == {
        "enabled": True,
        "primaryCount": 2,
        "exitCount": 1,
    }


def test_admin_can_generate_and_fuse_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_token", "test-admin-secret")
    headers = {"Authorization": "Bearer test-admin-secret"}
    with TestClient(app) as client:
        generated = client.post("/api/v1/admin/cards/generate", headers=headers, json={"count": 2, "quota": 7, "days": 30, "remark": "test"})
        codes = [item["code"] for item in generated.json()["cards"]]
        fused = client.post("/api/v1/admin/cards/fuse", headers=headers, json={"codes": codes, "remark": "merged"})

    assert generated.status_code == 201
    assert fused.status_code == 201
    assert fused.json()["card"]["quota"] == 14
    assert len(fused.json()["card"]["code"]) > 10


@pytest.mark.asyncio
async def test_distributed_task_creation_reserves_quota_idempotently() -> None:
    await create_schema()
    cdk = "TL-TEST-DISTRIBUTED-CARD"
    request_id = "test-idempotency-key"
    async with SessionLocal() as session:
        await session.execute(delete(QuotaLedger).where(QuotaLedger.request_id == request_id))
        await session.execute(delete(Task).where(Task.request_id == request_id))
        card = await session.scalar(select(QuotaCard).where(QuotaCard.code_hash == owner_key(cdk)))
        if not card:
            card = QuotaCard(code_hash=owner_key(cdk), code_mask=mask_owner(cdk), quota=10)
            session.add(card)
        card.used_quota = 0
        card.reserved_quota = 0
        card.status = "active"
        await session.commit()

        service = TaskService(session)
        payload = TaskCreate(mode="paypal", cardCode=cdk, proxyMode="custom", proxiesText="http://127.0.0.1:8080", accounts=[AccountInput(at="eyJ.test.token-value")])
        first = await service.create(payload, request_id)
        second = await service.create(payload, request_id)
        await session.refresh(card)

        assert first["id"] == second["id"]
        assert card.reserved_quota == 1
        assert first["progress"]["total"] == 1
        outbox = await session.scalar(select(OutboxEvent).where(OutboxEvent.message_key == first["id"]))
        assert outbox is not None
        assert outbox.status == "pending"


@pytest.mark.asyncio
async def test_legacy_card_is_adopted_into_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    code = "TL-LEGACY-BRIDGE-7K4Q"

    async def fake_check_card(value: str) -> dict[str, object]:
        assert value == code
        return {"ok": True, "quota": 12, "usedQuota": 3, "reservedQuota": 1}

    monkeypatch.setattr("app.card_sync.legacy_client.check_card", fake_check_card)

    class FakeSession:
        card: QuotaCard | None = None

        async def scalar(self, _query):
            return self.card

        def add(self, value: QuotaCard) -> None:
            self.card = value

        async def flush(self) -> None:
            return None

    session = FakeSession()
    card, imported = await resolve_card(session, code)
    assert imported is True
    assert card is not None
    assert card.code_value == code
    assert card.quota == 12
    assert card.used_quota == 3
    assert card.reserved_quota == 1
