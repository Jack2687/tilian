import json

import pytest

from app.main import parse_accounts
from app.models import QuotaCard, Task
from app.schemas import AccountInput, AccountParse, TaskCreate
from app.security import decrypt_account_credential, encrypt_account_credential, encrypt_credential
from app.services.tasks import TaskService, task_proxy_pools


def task_input(**changes):
    return TaskCreate(**{"mode": "momo", "cardCode": "test-card", "accounts": [{"at": "test-access-token"}], **changes})


@pytest.mark.parametrize("mode", ["paypal", "gopay", "gcash", "momo", "upi", "blik", "pix", "ideal", "card"])
def test_builtin_pool_is_required_for_every_channel(mode):
    data = task_input(mode=mode, proxiesText="http://caller-pool:8080")
    for config in (None, {}, {"channels": {mode: {"enabled": False, "primaryText": "http://disabled:8080"}}}):
        with pytest.raises(ValueError, match="管理员代理池未开启"):
            task_proxy_pools(data, config)
    with pytest.raises(ValueError, match="建单代理池不能为空"):
        task_proxy_pools(data, {"channels": {mode: {"enabled": True, "primaryText": ""}}})


def test_builtin_uses_only_selected_administrator_pool():
    primary, exits = task_proxy_pools(
        task_input(proxiesText="http://caller:8080", exitProxiesText="http://caller-exit:8080"),
        {"channels": {"momo": {"enabled": True, "primaryText": "proxy.test:8080:user:pass", "exitText": "http://exit:8081"}}},
    )
    assert primary == "http://user:pass@proxy.test:8080"
    assert exits == "http://exit:8081"


def test_custom_local_proxy_remains_supported_and_empty_primary_is_rejected():
    assert task_proxy_pools(task_input(proxyMode="custom", proxiesText="127.0.0.1:7890")) == ("http://127.0.0.1:7890", "")
    with pytest.raises(ValueError, match="建单代理池不能为空"):
        task_proxy_pools(task_input(proxyMode="custom", exitProxiesText="http://exit:8080"))


@pytest.mark.asyncio
async def test_bad_pool_is_rejected_before_card_or_quota_changes(monkeypatch):
    class Session:
        async def scalar(self, _query):
            return None

        async def get(self, *_args):
            return None

    async def unexpected_card_lookup(*_args, **_kwargs):
        pytest.fail("Invalid proxy input must be rejected before touching quota")

    monkeypatch.setattr("app.services.tasks.resolve_card", unexpected_card_lookup)
    with pytest.raises(ValueError, match="管理员代理池未开启"):
        await TaskService(Session()).create(task_input(), "bad-pool-test")


@pytest.mark.parametrize("field", ["sessionToken", "session_token", "sessionCookie", "session_cookie", "__Secure-next-auth.session-token"])
@pytest.mark.asyncio
async def test_session_json_import_preserves_nested_per_account_session(field, monkeypatch):
    monkeypatch.setattr("app.main.settings.execution_mode", "distributed")
    data = {"account": {"access_token": "test-access-token"}, "tokens": {field: "session-secret"}, "user": {"email": "account@example.test"}}
    parsed = await parse_accounts(AccountParse(text=json.dumps(data, indent=2)), _user=None)
    assert parsed["count"] == 1
    assert parsed["items"][0]["at"] == "test-access-token"
    assert parsed["items"][0]["sessionToken"] == "session-secret"
    assert parsed["items"][0]["email"] == "account@example.test"


def test_account_credentials_support_legacy_tokens_and_encrypted_sessions():
    assert decrypt_account_credential(encrypt_credential("legacy-at")) == ("legacy-at", "")
    encrypted = encrypt_account_credential("account-at", "account-session")
    assert b"account-at" not in encrypted
    assert b"account-session" not in encrypted
    assert decrypt_account_credential(encrypted) == ("account-at", "account-session")
    assert AccountInput(at="account-at", session_token="account-session").sessionToken == "account-session"


@pytest.mark.asyncio
async def test_task_keeps_session_only_in_encrypted_account_credential(monkeypatch):
    card = QuotaCard(quota=2, used_quota=0, reserved_quota=0, status="active", version=0)

    class Session:
        task = None

        async def scalar(self, _query):
            return None

        async def get(self, *_args):
            return None

        def add(self, row):
            if isinstance(row, Task):
                self.task = row

        async def commit(self):
            pass

        async def refresh(self, _row):
            pass

    async def find_card(*_args, **_kwargs):
        return card, False

    monkeypatch.setattr("app.services.tasks.resolve_card", find_card)
    session = Session()
    result = await TaskService(session).create(
        task_input(proxyMode="custom", proxiesText="127.0.0.1:7890", accounts=[{"at": "account-at", "sessionToken": "account-session"}]),
        "session-test",
    )
    assert decrypt_account_credential(session.task.items[0].credential) == ("account-at", "account-session")
    assert "account-session" not in json.dumps(session.task.payload)
    assert "account-session" not in json.dumps(result)
    assert card.reserved_quota == 1
