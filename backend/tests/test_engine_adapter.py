import json

from app.engine_adapter import EngineAdapter, ROOT


ADAPTER = EngineAdapter()
PRIMARY = "http://primary:8080"
EXIT = "http://exit:8080"
FINGERPRINT = {"kernel": "chrome", "ua": "Test UA", "timezone": "Asia/Shanghai"}
FINGERPRINT_JSON = json.dumps(FINGERPRINT, ensure_ascii=False, separators=(",", ":"))


def command_tail(mode: str, payload: dict, proxy: str = PRIMARY, exit_proxy: str = EXIT) -> list[str]:
    command = ADAPTER.command(mode, "at-token", "user@example.com", proxy, payload, exit_proxy)
    assert command[0]
    return command[1:]


def assert_flag_value(command: list[str], flag: str, value: str) -> None:
    assert command.count(flag) == 1
    assert command[command.index(flag) + 1] == value


def test_paypal_command_contract() -> None:
    payload = {"params": {"country": "US", "fingerprint": FINGERPRINT}}
    assert command_tail("paypal", payload) == [
        str(ROOT / "engines/sms_tool/link_service.py"),
        "--at",
        "at-token",
        "--mode",
        "paypal",
        "--json",
        "",
        "--proxy",
        PRIMARY,
        "--exit-proxy",
        EXIT,
        "--fingerprint",
        FINGERPRINT_JSON,
        "--country",
        "US",
        "--auto",
    ]


def test_card_command_contract() -> None:
    payload = {
        "params": {
            "country": "PH",
            "currency": "PHP",
            "fingerprint": FINGERPRINT,
            "stickyProxy": True,
            "checkoutUiMode": "popup",
            "promoCampaignId": "promo-card",
            "forceZero": True,
            "timeoutMs": 9500,
        },
        "proxiesText": f"{PRIMARY}\nhttp://primary-two:8081",
        "exitProxiesText": f"{EXIT}\nhttp://exit-two:8081",
    }
    assert command_tail("card", payload) == [
        str(ROOT / "card_engine/card_service.py"),
        "--at",
        "at-token",
        "--country",
        "PH",
        "--currency",
        "PHP",
        "--json",
        "",
        "--proxy",
        PRIMARY,
        "--exit-proxy",
        EXIT,
        "--fingerprint",
        FINGERPRINT_JSON,
        "--sticky-proxy",
        "1",
        "--proxy-pool",
        f'["{PRIMARY}","http://primary-two:8081"]',
        "--exit-pool",
        f'["{EXIT}","http://exit-two:8081"]',
        "--checkout-ui-mode",
        "popup",
        "--promo-campaign-id",
        "promo-card",
        "--force-zero",
        "--timeout",
        "10",
    ]


def test_gopay_command_contract() -> None:
    payload = {
        "params": {
            "country": "ID",
            "currency": "IDR",
            "fingerprint": FINGERPRINT,
            "checkoutUiMode": "web",
            "promoCampaignId": "promo-gopay",
            "requireZero": True,
            "forceMethod": True,
            "forceZero": True,
            "gopayBrowserFallback": True,
        },
        "proxiesText": PRIMARY,
    }
    command = command_tail("gopay", payload)
    assert command == [
        str(ROOT / "gopay_engine/gopay_service.py"),
        "--at",
        "at-token",
        "--proxy",
        PRIMARY,
        "--json",
        "",
        "--country",
        "ID",
        "--currency",
        "IDR",
        "--fingerprint",
        FINGERPRINT_JSON,
        "--proxy-pool",
        f'["{PRIMARY}"]',
        "--exit-proxy",
        EXIT,
        "--promo-campaign-id",
        "promo-gopay",
        "--checkout-ui-mode",
        "web",
        "--require-zero",
        "--force-payment-method",
        "--force-zero",
        "--gopay-browser-fallback",
        "1",
    ]
    assert "--sticky-proxy" not in command


def test_card_defaults_to_sticky_proxy_but_allows_explicit_opt_out() -> None:
    command = command_tail("card", {"params": {}})
    assert_flag_value(command, "--sticky-proxy", "1")
    assert "--sticky-proxy" not in command_tail("card", {"params": {"stickyProxy": False}})


def test_gcash_command_and_environment_contract() -> None:
    payload = {
        "params": {
            "country": "PH",
            "currency": "PHP",
            "fingerprint": FINGERPRINT,
            "checkoutUiMode": "web",
            "promoCampaignId": "promo-gcash",
            "requireZero": True,
            "forcePaymentMethod": True,
            "forceZero": True,
        },
        "proxiesText": PRIMARY,
    }
    command = command_tail("gcash", payload)
    assert command == [
        str(ROOT / "gcash_engine/gcash_service.py"),
        "--at",
        "at-token",
        "--proxy",
        PRIMARY,
        "--json",
        "",
        "--country",
        "PH",
        "--currency",
        "PHP",
        "--fingerprint",
        FINGERPRINT_JSON,
        "--proxy-pool",
        f'["{PRIMARY}"]',
        "--exit-proxy",
        EXIT,
        "--promo-campaign-id",
        "promo-gcash",
        "--checkout-ui-mode",
        "web",
        "--require-zero",
    ]
    assert "--sticky-proxy" not in command
    enabled = ADAPTER.environment("gcash", payload)
    assert enabled["GCASH_FORCE_CPMT"] == "1"
    assert enabled["GCASH_FORCE_ZERO"] == "1"
    disabled = ADAPTER.environment("gcash", {"params": {"requireZero": False}})
    assert disabled["GCASH_FORCE_CPMT"] == "0"
    assert disabled["GCASH_FORCE_ZERO"] == "0"


def test_momo_command_contract() -> None:
    payload = {
        "params": {
            "country": "VN",
            "currency": "VND",
            "sessionToken": "session-value",
            "fingerprint": FINGERPRINT,
            "checkoutUiMode": "web",
            "forcePaymentMethod": True,
            "forceZero": True,
        }
    }
    assert command_tail("momo", payload) == [
        str(ROOT / "momo_engine/momo_service.py"),
        "--at",
        "at-token",
        "--proxy",
        PRIMARY,
        "--exit-proxy",
        EXIT,
        "--country",
        "VN",
        "--currency",
        "VND",
        "--promo-campaign-id",
        "plus-1-month-free",
        "--session-token",
        "session-value",
        "--require-zero",
        "--force-payment-method",
        "--force-zero",
        "--checkout-ui-mode",
        "web",
        "--fingerprint",
        FINGERPRINT_JSON,
    ]


def test_blik_command_contract() -> None:
    payload = {"params": {"fingerprint": FINGERPRINT, "promoCampaignId": "promo-blik", "paidTimeout": 12}}
    assert command_tail("blik", payload) == [
        str(ROOT / "blik_engine/blik_service.py"),
        "--at",
        "at-token",
        "--proxy",
        PRIMARY,
        "--email",
        "user@example.com",
        "--fingerprint",
        FINGERPRINT_JSON,
        "--promo-campaign-id",
        "promo-blik",
        "--paid-timeout",
        "30",
    ]


def test_upi_command_contract_keeps_source_proxy_roles() -> None:
    # UPI is a single-chain link type in the source worker: the batch endpoints
    # never accept an exit pool and submit `exit_proxies` as a copy of
    # `entry_proxies`.  The promotion preflight and the Checkout/Stripe chain
    # therefore share one India egress.
    payload = {
        "params": {
            "checkoutUiMode": "web",
            "promoCampaignId": "promo-upi",
            "forcePaymentMethod": True,
            "forceZero": True,
            "waitPaid": True,
            "paidTimeout": 12,
            "fingerprint": FINGERPRINT,
        }
    }
    command = command_tail("upi", payload)
    assert command == [
        str(ROOT / "engines/pay153-checkout-link-main/upi_cli.py"),
        "--at",
        "at-token",
        "--proxy",
        PRIMARY,
        "--promotion-proxy",
        PRIMARY,
        "--email",
        "user@example.com",
        "--promo-campaign-id",
        "promo-upi",
        "--checkout-ui-mode",
        "web",
        "--upi-rail",
        "upi-git5",
        "--force-payment-method",
        "--force-zero",
        "--wait-paid",
        "--paid-timeout",
        "30",
        "--fingerprint",
        FINGERPRINT_JSON,
    ]
    assert "--proxy-pool" not in command


def test_upi_command_forwards_billing_json() -> None:
    billing = {
        "name": "Test User",
        "email": "user@example.com",
        "line1": "1 Main Road",
        "city": "Bengaluru",
        "state": "KA",
        "postal_code": "560001",
    }
    payload = {"params": {"billing": billing}}
    command = command_tail("upi", payload)
    assert command[command.index("--billing-json") + 1] == json.dumps(
        billing, ensure_ascii=False, separators=(",", ":")
    )


def test_pix_command_contract() -> None:
    payload = {
        "params": {
            "country": "BR",
            "currency": "BRL",
            "billing": {"email": "", "taxId": "tax-1"},
            "fingerprint": FINGERPRINT,
            "promoCampaignId": "promo-pix",
            "requireZero": False,
            "forceMethod": True,
            "forceZero": True,
            "maxPollAttempts": 2.2,
            "waitPaid": True,
            "paidTimeout": 5,
        },
        "proxiesText": PRIMARY,
    }
    command = command_tail("pix", payload)
    assert command == [
        str(ROOT / "pix_engine/pix_cli.py"),
        "--at",
        "at-token",
        "--proxy",
        PRIMARY,
        "--country",
        "BR",
        "--currency",
        "BRL",
        "--proxy-pool",
        f'["{PRIMARY}"]',
        "--billing-json",
        '{"email":"user@example.com","taxId":"tax-1"}',
        "--fingerprint",
        FINGERPRINT_JSON,
        "--promo-campaign-id",
        "promo-pix",
        "--no-require-zero",
        "--force-payment-method",
        "--force-zero",
        "--max-poll-attempts",
        "3",
        "--wait-paid",
        "--paid-timeout",
        "30",
    ]
    assert "--exit-proxy" not in command


def test_ideal_command_always_has_explicit_sticky_proxy() -> None:
    payload = {
        "params": {
            "country": "NL",
            "currency": "EUR",
            "fingerprint": FINGERPRINT,
            "promoCampaignId": "promo-ideal",
            "requireZero": False,
            "forceMethod": True,
            "forceZero": True,
            "idealBank": "ING",
            "stickyProxy": False,
            "timeoutMs": 9100,
        },
        "proxiesText": PRIMARY,
    }
    command = command_tail("ideal", payload)
    assert command == [
        str(ROOT / "ideal_engine/ideal_service.py"),
        "--at",
        "at-token",
        "--proxy",
        PRIMARY,
        "--json",
        "",
        "--proxy-pool",
        f'["{PRIMARY}"]',
        "--exit-proxy",
        EXIT,
        "--country",
        "NL",
        "--currency",
        "EUR",
        "--promo-campaign-id",
        "promo-ideal",
        "--no-require-zero",
        "--force-payment-method",
        "--force-zero",
        "--ideal-bank",
        "ING",
        "--email",
        "user@example.com",
        "--timeout",
        "10",
        "--sticky-proxy",
        "0",
        "--fingerprint",
        FINGERPRINT_JSON,
    ]
    default_command = command_tail("ideal", {"params": {}}, proxy="", exit_proxy="")
    assert_flag_value(default_command, "--sticky-proxy", "1")


def test_strict_success_rules() -> None:
    blik = ADAPTER._normalize_result(
        "blik",
        {"ok": True, "link": "https://blik.example", "paid": False, "payment_status": "pending"},
    )
    assert blik["ok"] is False
    assert blik["link"] == ""

    upi_not_zero = ADAPTER._normalize_result(
        "upi",
        {"ok": True, "link": "https://upi.example", "checkout_amount": 1, "promo_applied": True},
    )
    assert upi_not_zero["ok"] is False
    assert upi_not_zero["link"] == ""

    upi_zero = ADAPTER._normalize_result(
        "upi",
        {
            "ok": True,
            "link": "https://payments.stripe.com/upi/instructions/fixture",
            "checkout_amount": 0,
            "promo_applied": True,
        },
    )
    assert upi_zero["ok"] is True
    assert upi_zero["link"] == "https://payments.stripe.com/upi/instructions/fixture"

    ideal_not_zero = ADAPTER._normalize_result(
        "ideal",
        {"ok": True, "link": "https://ideal.example", "checkout_amount": 0, "promo_applied": False},
    )
    assert ideal_not_zero["ok"] is False
    assert ideal_not_zero["link"] == ""

    ideal_zero = ADAPTER._normalize_result(
        "ideal",
        {"ok": True, "link": "https://ideal.example", "checkout_amount": 0, "promo_applied": True},
    )
    assert ideal_zero["ok"] is True
    assert ideal_zero["link"] == "https://ideal.example"


def test_ok_false_never_returns_link_for_any_channel() -> None:
    for mode in ("paypal", "card", "gopay", "gcash", "momo", "upi", "blik", "pix", "ideal"):
        result = ADAPTER._normalize_result(
            mode,
            {
                "ok": False,
                "link": "https://must-not-pass.example",
                "ba_url": "https://must-not-pass.example",
                "hosted_instructions_url": "https://must-not-pass.example",
                "provider_redirect_url": "https://must-not-pass.example",
            },
        )
        assert result["ok"] is False
        assert result["link"] == ""
        assert result["reason_code"]
        assert result["error"]
