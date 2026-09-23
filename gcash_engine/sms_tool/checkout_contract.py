"""Shared request and response contracts for ChatGPT Checkout and Stripe init."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from html import unescape as html_unescape
from typing import Any, Iterable


CHECKOUT_PATH = "/backend-api/payments/checkout"
CHECKOUT_URL = f"https://chatgpt.com{CHECKOUT_PATH}"
STRIPE_INIT_URL = "https://api.stripe.com/v1/payment_pages/{checkout_session_id}/init"
DEFAULT_STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)


class CheckoutContractError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "checkout_contract_invalid",
        error_stage: str = "checkout_contract",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.error_stage = error_stage
        self.retryable = retryable


@dataclass(frozen=True)
class PaymentMethodProfile:
    key: str
    stripe_type: str
    country: str
    currency: str
    payment_locale: str
    browser_locale: str
    browser_timezone: str


PAYMENT_METHOD_PROFILES: dict[str, PaymentMethodProfile] = {
    "paypal": PaymentMethodProfile("paypal", "paypal", "US", "USD", "en", "en-US", "America/New_York"),
    "upi": PaymentMethodProfile("upi", "upi", "IN", "INR", "en", "en-IN", "Asia/Kolkata"),
    "ideal": PaymentMethodProfile("ideal", "ideal", "NL", "EUR", "nl", "nl-NL", "Europe/Amsterdam"),
    "pix": PaymentMethodProfile("pix", "pix", "BR", "BRL", "pt-BR", "pt-BR", "America/Sao_Paulo"),
    "kakao": PaymentMethodProfile("kakao", "kakao_pay", "KR", "KRW", "ko", "ko-KR", "Asia/Seoul"),
    "blik": PaymentMethodProfile("blik", "blik", "PL", "PLN", "pl", "pl-PL", "Europe/Warsaw"),
    "twint": PaymentMethodProfile("twint", "twint", "CH", "CHF", "de", "de-CH", "Europe/Zurich"),
    "direct_card": PaymentMethodProfile("direct_card", "card", "PH", "PHP", "en", "en-PH", "Asia/Manila"),
    "momo": PaymentMethodProfile("momo", "momo", "VN", "VND", "vi", "vi-VN", "Asia/Ho_Chi_Minh"),
    "gopay": PaymentMethodProfile("gopay", "gopay", "ID", "IDR", "id", "id-ID", "Asia/Jakarta"),
    "gcash": PaymentMethodProfile("gcash", "gcash", "PH", "PHP", "en-PH", "en-PH", "Asia/Manila"),
    "grabpay": PaymentMethodProfile("grabpay", "grabpay", "PH", "PHP", "en-PH", "en-PH", "Asia/Manila"),
}


@dataclass(frozen=True)
class CheckoutRequestContract:
    payment_method: str
    billing_country: str
    currency: str
    payment_locale: str
    browser_locale: str
    browser_timezone: str
    entry_point: str = "all_plans_pricing_modal"
    plan_name: str = "chatgptplusplan"
    promo_campaign_id: str = "plus-1-month-free"
    checkout_ui_mode: str = "custom"
    # 建单时显式指定的支付方式（如 GCash 传 ["gcash"]，确保 custom_payment_methods 下发）
    payment_method_types: tuple[str, ...] = ()

    @classmethod
    def for_payment_method(
        cls,
        payment_method: str,
        *,
        billing_country: str = "",
        currency: str = "",
        payment_locale: str = "",
        browser_locale: str = "",
        browser_timezone: str = "",
        promo_campaign_id: str = "plus-1-month-free",
        checkout_ui_mode: str = "custom",
        payment_method_types: tuple[str, ...] | list[str] | None = None,
    ) -> "CheckoutRequestContract":
        key = str(payment_method or "").strip().lower().replace("-", "_")
        profile = PAYMENT_METHOD_PROFILES.get(key)
        if profile is None:
            raise CheckoutContractError(f"unsupported payment method contract: {payment_method}")
        contract = cls(
            payment_method=profile.key,
            billing_country=str(billing_country or profile.country).strip().upper(),
            currency=str(currency or profile.currency).strip().upper(),
            payment_locale=str(payment_locale or profile.payment_locale).strip(),
            browser_locale=str(browser_locale or profile.browser_locale).strip(),
            browser_timezone=str(browser_timezone or profile.browser_timezone).strip(),
            promo_campaign_id=str(promo_campaign_id or "").strip(),
            checkout_ui_mode=str(checkout_ui_mode or "custom").strip(),
            payment_method_types=tuple(dict.fromkeys(str(t).strip() for t in (payment_method_types or ()) if str(t).strip())),
        )
        contract.validate()
        return contract

    @property
    def stripe_payment_method(self) -> str:
        return PAYMENT_METHOD_PROFILES[self.payment_method].stripe_type

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Z]{2}", self.billing_country):
            raise CheckoutContractError("billing_country must be a two-letter country code")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise CheckoutContractError("currency must be a three-letter currency code")
        if self.checkout_ui_mode not in {"custom", "hosted"}:
            raise CheckoutContractError("checkout_ui_mode must be custom or hosted")

    def checkout_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "plan_name": self.plan_name,
            "billing_details": {
                "country": self.billing_country,
                "currency": self.currency,
            },
            "checkout_ui_mode": self.checkout_ui_mode,
        }
        if self.checkout_ui_mode == "custom":
            payload["entry_point"] = self.entry_point
        else:
            payload["cancel_url"] = "https://chatgpt.com/#pricing"
        # 显式支付方式（GCash 传 ["gcash"]）→ 建单即下发对应 custom_payment_methods
        if self.payment_method_types:
            payload["payment_method_types"] = list(self.payment_method_types)
        if self.promo_campaign_id:
            payload["promo_campaign"] = {
                "promo_campaign_id": self.promo_campaign_id,
                "is_coupon_from_query_param": False,
            }
        return payload

    def stripe_init_payload(
        self,
        publishable_key: str,
        *,
        stripe_version: str = DEFAULT_STRIPE_VERSION,
        stripe_js_id: str = "",
    ) -> dict[str, str]:
        key = str(publishable_key or "").strip()
        if not key.startswith("pk_"):
            raise CheckoutContractError(
                "checkout response did not contain a Stripe publishable key",
                error_code="checkout_publishable_key_missing",
                error_stage="checkout_response",
            )
        return {
            "browser_locale": self.browser_locale,
            "browser_timezone": self.browser_timezone,
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id or str(uuid.uuid4()),
            "elements_session_client[locale]": self.payment_locale,
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": key,
            "_stripe_version": stripe_version,
        }


@dataclass(frozen=True)
class CheckoutSessionContract:
    checkout_session_id: str
    processor_entity: str
    publishable_key: str
    raw_payload: Any = None

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        billing_country: str,
        fallback_publishable_key: str = "",
    ) -> "CheckoutSessionContract":
        if not isinstance(payload, dict):
            raise CheckoutContractError(
                "checkout response must be a JSON object",
                error_code="checkout_bad_response",
                error_stage="checkout_response",
                retryable=True,
            )
        session_id = str(
            payload.get("checkout_session_id") or payload.get("session_id") or payload.get("id") or ""
        ).strip()
        if not session_id.startswith(("cs_", "oaics_")):
            raise CheckoutContractError(
                "checkout response did not contain a supported session id",
                error_code="checkout_session_missing",
                error_stage="checkout_response",
                retryable=True,
            )
        processor = str(payload.get("processor_entity") or "").strip()
        if not processor:
            processor = "openai_llc" if billing_country.upper() == "US" else "openai_ie"
        publishable_key = str(payload.get("publishable_key") or fallback_publishable_key or "").strip()
        return cls(session_id, processor, publishable_key, payload)


@dataclass(frozen=True)
class StripeCapabilityEvidence:
    amount_minor: int | None
    currency: str
    payment_method_types: tuple[str, ...]
    ordered_payment_method_types: tuple[str, ...]
    custom_payment_methods: tuple[str, ...]
    offer_state: str

    @classmethod
    def from_payload(cls, payload: Any, *, fallback_currency: str = "") -> "StripeCapabilityEvidence":
        if not isinstance(payload, dict):
            raise CheckoutContractError(
                "Stripe init response must be a JSON object",
                error_code="stripe_init_bad_response",
                error_stage="stripe_init",
                retryable=True,
            )
        standard = _collect_method_group(payload, "payment_method_types")
        ordered = _collect_method_group(payload, "ordered_payment_method_types")
        custom = _collect_method_group(payload, "custom_payment_methods")
        methods = tuple(_dedupe((*standard, *ordered, *custom)))
        amount = _extract_amount_minor(payload)
        currency = _extract_currency(payload) or str(fallback_currency or "").upper()
        offer_state = "zero_due" if amount == 0 else "nonzero_due" if amount is not None else "unknown_amount"
        return cls(amount, currency, methods, tuple(ordered), tuple(custom), offer_state)

    def classification_for(self, stripe_payment_method: str) -> tuple[str, bool | None]:
        expected = normalize_payment_method_token(stripe_payment_method)
        if expected in self.payment_method_types:
            return "eligible", True
        if self.payment_method_types:
            return "ineligible", False
        return "unknown", None


def normalize_payment_method_token(value: Any) -> str:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "kakao": "kakao_pay",
        "card_payment": "card",
        "direct_card": "card",
        "go_pay": "gopay",
        "grab_pay": "grabpay",
    }
    return aliases.get(token, token)


def _collect_method_group(payload: Any, key: str) -> list[str]:
    values: list[str] = []
    for group in _values_for_key(payload, key):
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, str):
                token = normalize_payment_method_token(item)
                if token:
                    values.append(token)
            elif isinstance(item, dict):
                for candidate_key in ("type", "payment_method_type", "name", "id"):
                    candidate = item.get(candidate_key)
                    token = normalize_payment_method_token(candidate)
                    if token:
                        values.append(token)
                        break
    return _dedupe(values)


def _values_for_key(value: Any, target: str, *, depth: int = 0) -> Iterable[Any]:
    if depth > 10:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == target:
                yield item
            if isinstance(item, (dict, list)):
                yield from _values_for_key(item, target, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                yield from _values_for_key(item, target, depth=depth + 1)


def _extract_amount_minor(payload: dict[str, Any]) -> int | None:
    paths = (
        ("total_summary", "due"),
        ("invoice", "amount_due"),
        ("elements_options", "amount"),
        ("payment_intent", "amount"),
        ("amount_due",),
        ("checkout_state", "total", "total"),
        ("checkoutState", "total", "total"),
        ("checkout_state", "total", "subtotal"),
        ("checkoutState", "total", "subtotal"),
        ("total", "total"),
        ("total", "due"),
    )
    for path in paths:
        value: Any = payload
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        parsed = _minor_units(value)
        if parsed is not None:
            return parsed
    for path in paths:
        parsed = _find_nested_amount(payload, path)
        if parsed is not None:
            return parsed
    return None


def _find_nested_amount(value: Any, path: tuple[str, ...], *, depth: int = 0, seen: set[int] | None = None) -> int | None:
    """Find a known amount path inside the nested checkout/update envelope."""
    if depth > 10 or not path:
        return _minor_units(value) if not path else None
    if seen is None:
        seen = set()
    if isinstance(value, dict):
        identity = id(value)
        if identity in seen:
            return None
        seen.add(identity)
        key = path[0]
        if key in value:
            parsed = _find_nested_amount(value[key], path[1:], depth=depth + 1, seen=seen)
            if parsed is not None:
                return parsed
        for item in value.values():
            parsed = _find_nested_amount(item, path, depth=depth + 1, seen=seen)
            if parsed is not None:
                return parsed
    elif isinstance(value, list):
        identity = id(value)
        if identity in seen:
            return None
        seen.add(identity)
        for item in value:
            parsed = _find_nested_amount(item, path, depth=depth + 1, seen=seen)
            if parsed is not None:
                return parsed
    return None


def _minor_units(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    if isinstance(value, dict):
        for key in ("minorUnitsAmount", "minor_units_amount", "amount", "value", "unit_amount", "amount_minor"):
            if key in value and (parsed := _minor_units(value[key])) is not None:
                return parsed
    return None


def _extract_currency(payload: Any) -> str:
    for key in ("currency", "currency_code"):
        for value in _values_for_key(payload, key):
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value.strip()):
                return value.strip().upper()
    return ""


def extract_amount_from_checkout_html(html: Any) -> tuple[int | None, str]:
    """Extract the latest due amount from a rendered Checkout state page."""
    state = _checkout_state_from_html(str(html or ""))
    amount = _checkout_amount_minor(state)
    currency = _checkout_currency(state)
    if amount is not None:
        return amount, currency

    # Keep a small fallback for pages that expose a plain JSON snapshot.
    text = str(html or "")
    amount_matches = list(re.finditer(
        r'(?i)["\'](?:amount_due|amountDue|due)["\']\s*:\s*["\']?(-?\d+)',
        text,
    ))
    if not amount_matches:
        return None, ""
    amount = int(amount_matches[-1].group(1))
    currency_matches = list(re.finditer(
        r'(?i)["\'](?:currency|currency_code|currencyCode)["\']\s*:\s*["\']?([A-Z]{3})',
        text,
    ))
    currency = currency_matches[-1].group(1).upper() if currency_matches else ""
    return amount, currency


def _checkout_amount_minor(data: Any) -> int | None:
    wrappers = (
        "checkout_session", "checkoutSession", "session", "checkout",
        "data", "result", "payload", "response", "checkout_state",
        "checkoutState", "checkout_snapshot", "checkoutSnapshot",
    )
    paths = (
        ("checkout_amount_minor",),
        ("total_summary", "due"),
        ("totalSummary", "due"),
        ("invoice", "amount_due"),
        ("invoice", "amountDue"),
        ("amount_due",),
        ("amountDue",),
        ("amount_total",),
        ("amountTotal",),
        ("total", "total"),
        ("total", "due"),
        ("total", "taxInclusive"),
        ("total", "taxInclusiveAmount"),
    )
    visited: set[int] = set()

    def value_at(payload: Any, path: tuple[str, ...]) -> Any:
        current = payload
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def money(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, dict):
            for key in ("minorUnitsAmount", "minor_units_amount", "amount", "value", "unit_amount"):
                if key in value:
                    parsed = money(value[key])
                    if parsed is not None:
                        return parsed
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(round(value))
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            return int(value.strip())
        return None

    def find(payload: Any) -> int | None:
        if not isinstance(payload, dict) or id(payload) in visited:
            return None
        visited.add(id(payload))
        for path in paths:
            parsed = money(value_at(payload, path))
            if parsed is not None:
                return parsed
        for key in ("lineItems", "line_items"):
            items = payload.get(key)
            if isinstance(items, list):
                total = 0
                found = False
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    for item_key in ("total", "subtotal", "unitAmount", "unit_amount"):
                        parsed = money(item.get(item_key))
                        if parsed is not None:
                            total += parsed
                            found = True
                            break
                if found:
                    return total
        for key in wrappers:
            parsed = find(payload.get(key))
            if parsed is not None:
                return parsed
        return None

    return find(data)


def _checkout_currency(data: Any) -> str:
    visited: set[int] = set()
    wrappers = (
        "checkout_state", "checkoutState", "checkout_snapshot",
        "checkoutSnapshot", "checkout_session", "checkoutSession",
        "session", "checkout", "data", "result", "payload", "response",
        "total", "total_summary", "totalSummary",
    )

    def find(payload: Any) -> str:
        if not isinstance(payload, dict) or id(payload) in visited:
            return ""
        visited.add(id(payload))
        for key in ("currency", "currency_code", "currencyCode"):
            value = payload.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value.strip()):
                return value.strip().upper()
        for key in wrappers:
            value = find(payload.get(key))
            if value:
                return value
        return ""

    return find(data)


def _decode_react_router_payload(payload: Any) -> Any:
    if not isinstance(payload, list):
        return None
    resolved: dict[int, Any] = {}

    def resolve(reference: Any, depth: int = 0) -> Any:
        if not isinstance(reference, int):
            return reference
        if depth > 64:
            return None
        if reference < 0 or reference >= len(payload):
            return None
        if reference in resolved:
            return resolved[reference]
        value = payload[reference]
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            resolved[reference] = output
            for encoded_key, encoded_value in value.items():
                key_text = str(encoded_key)
                if not key_text.startswith("_") or not key_text[1:].isdigit():
                    continue
                key = resolve(int(key_text[1:]), depth + 1)
                if key is not None:
                    output[str(key)] = resolve(encoded_value, depth + 1)
            return output
        if isinstance(value, list):
            output_list: list[Any] = []
            resolved[reference] = output_list
            output_list.extend(resolve(item, depth + 1) for item in value)
            return output_list
        resolved[reference] = value
        return value

    return resolve(0)


def _checkout_state_from_html(html: str) -> dict[str, Any]:
    def find_state(
        payload: Any,
        *,
        visited: set[int] | None = None,
        depth: int = 0,
    ) -> dict[str, Any]:
        if depth > 32:
            return {}
        if visited is None:
            visited = set()
        if isinstance(payload, (dict, list)):
            payload_id = id(payload)
            if payload_id in visited:
                return {}
            visited.add(payload_id)
        if isinstance(payload, dict):
            for key in ("checkout_state", "checkoutState"):
                state = payload.get(key)
                if isinstance(state, dict):
                    return state
            for value in payload.values():
                state = find_state(value, visited=visited, depth=depth + 1)
                if state:
                    return state
            if _checkout_amount_minor(payload) is not None and any(
                key in payload
                for key in ("total", "total_summary", "totalSummary", "lineItems", "line_items")
            ):
                return payload
        elif isinstance(payload, list):
            for value in payload:
                state = find_state(value, visited=visited, depth=depth + 1)
                if state:
                    return state
        return {}

    def state_from_serialized(serialized: str) -> dict[str, Any]:
        try:
            decoded = json.loads(serialized)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        if isinstance(decoded, list):
            state = find_state(_decode_react_router_payload(decoded))
            if state:
                return state
        return find_state(decoded)

    enqueue_pattern = re.compile(
        r'window\.__reactRouterContext\.streamController\.enqueue\(("(?:\\.|[^"\\])*")\)',
        re.DOTALL,
    )
    chunks: list[str] = []
    for match in enqueue_pattern.finditer(html):
        try:
            raw_payload = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(raw_payload, str):
            continue
        chunks.append(raw_payload)
        state = state_from_serialized(raw_payload)
        if state:
            return state
    if len(chunks) > 1:
        state = state_from_serialized("".join(chunks))
        if state:
            return state

    application_json_pattern = re.compile(
        r"<script\b[^>]*\btype=(['\"])application/json\1[^>]*>(.*?)</script>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in application_json_pattern.finditer(html):
        state = state_from_serialized(html_unescape(match.group(2)).strip())
        if state:
            return state
    return {}


def _dedupe(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output
