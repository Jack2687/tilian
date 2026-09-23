"""Shared orchestration core for redirect-based wallet payment methods.

The module deliberately owns no HTTP sessions.  A caller supplies a
``WalletProviderTransport`` which performs the wire calls described by the
typed requests below.  This keeps the protocol contract testable while letting
the desktop application retain ownership of proxies, cookies and TLS clients.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import html
import random
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import unquote, urlsplit

from .checkout_contract import (
    CheckoutContractError,
    CheckoutRequestContract,
    CheckoutSessionContract,
    PAYMENT_METHOD_PROFILES,
    StripeCapabilityEvidence,
    extract_amount_from_checkout_html,
)


_RUNTIME_VERSION = "6f8494a281"
_SIDE_EFFECT_STAGES = frozenset({"confirm", "approve", "poll", "follow_redirect"})
_TERMINAL_FAILURE_STATES = frozenset({"declined", "denied", "failed", "rejected"})
_TERMINAL_CANCEL_STATES = frozenset({"canceled", "cancelled"})

_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")
_PROXY_AUTH_RE = re.compile(r"(?i)\b(https?|socks5h?)://[^\s/@]+@")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|"
    r"publishable[_-]?key|password|authorization)\b[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"'&,}]+)"
)
_STRIPE_SECRET_RE = re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9_]{6,}\b")
_SESSION_ID_RE = re.compile(r"\b(?:cs|oaics|pm|pi|seti)_(?:live_|test_)?[A-Za-z0-9_]{8,}\b")
_GOPAY_PM_REDIRECT_RE = re.compile(
    r"https?://pm-redirects\.stripe\.com/authorize/acct_[A-Za-z0-9]+/(?:sa|pa)_nonce_[A-Za-z0-9]+",
    re.IGNORECASE,
)
_GOPAY_MIDTRANS_RE = re.compile(
    r"https?://app\.midtrans\.com/(?:snap/v[34]/redirection/[0-9a-f-]{36}|pm-redirects/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+)",
    re.IGNORECASE,
)
_DEFAULT_PROMOTION_ATTEMPTS = 3


@dataclass(frozen=True)
class WalletMethodSpec:
    """Wallet-specific behavior layered on the shared Checkout profile."""

    key: str
    label: str
    redirect_hosts: tuple[str, ...]

    @property
    def country(self) -> str:
        return PAYMENT_METHOD_PROFILES[self.key].country

    @property
    def currency(self) -> str:
        return PAYMENT_METHOD_PROFILES[self.key].currency

    @property
    def locale(self) -> str:
        return PAYMENT_METHOD_PROFILES[self.key].payment_locale

    @property
    def stripe_type(self) -> str:
        return PAYMENT_METHOD_PROFILES[self.key].stripe_type


WALLET_METHODS: dict[str, WalletMethodSpec] = {
    "gopay": WalletMethodSpec("gopay", "GoPay", ("pm-redirects.stripe.com", "gopay.co.id", "gojek.com", "midtrans.com")),
    "gcash": WalletMethodSpec("gcash", "GCash", ("gcash.com", "mynt.com")),
    "grabpay": WalletMethodSpec("grabpay", "GrabPay", ("grab.com", "grabpay.com")),
}


@dataclass(frozen=True)
class WalletFlowIdentifiers:
    stripe_js_id: str
    elements_session_id: str
    elements_session_config_id: str
    runtime_version: str = _RUNTIME_VERSION

    @classmethod
    def create(cls, uuid_factory: Callable[[], Any] = uuid.uuid4) -> "WalletFlowIdentifiers":
        stripe_js_id = str(uuid_factory())
        session_suffix = str(uuid_factory()).replace("-", "")[:11]
        config_id = str(uuid_factory())
        return cls(stripe_js_id, f"elements_session_{session_suffix}", config_id)


@dataclass(frozen=True)
class WalletTransportRequest:
    """A redaction-safe request envelope passed to the injected transport."""

    stage: str
    method: str
    contract: CheckoutRequestContract
    flow_id: str
    attempt: int = 1
    checkout_session_id: str = ""
    processor_entity: str = ""
    access_token: str = field(default="", repr=False)
    publishable_key: str = field(default="", repr=False)
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    auth_context: Mapping[str, Any] = field(default_factory=dict, repr=False)
    transport_context: Mapping[str, Any] = field(default_factory=dict, repr=False)
    redirect_url: str = field(default="", repr=False)


class WalletProviderTransport(Protocol):
    """Wire boundary implemented by the application HTTP/proxy layer."""

    def create_checkout(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def update_checkout_promotion(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def read_checkout_page(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def extract_provider_link(self, request: WalletTransportRequest) -> Mapping[str, Any] | str: ...

    def stripe_init(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def create_payment_method(self, request: WalletTransportRequest) -> Mapping[str, Any] | str: ...

    def confirm_payment(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def approve_checkout(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def poll_payment(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def extract_intent_redirect(self, request: WalletTransportRequest) -> Mapping[str, Any] | str: ...

    def follow_redirect(self, request: WalletTransportRequest) -> Mapping[str, Any] | str: ...


class WalletProviderError(RuntimeError):
    """Structured adapter failure safe to expose to callers and persistence."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "wallet_provider_failed",
        error_stage: str = "wallet_provider",
        retryable: bool = False,
        status: str = "failed",
    ) -> None:
        self.error_code = str(error_code or "wallet_provider_failed")
        self.error_stage = str(error_stage or "wallet_provider")
        self.retryable = bool(retryable)
        self.status = status if status in {"failed", "cancelled", "timed_out", "unknown"} else "unknown"
        super().__init__(redact_sensitive_text(message))


class WalletCancelledError(WalletProviderError):
    def __init__(self, message: str = "wallet flow was cancelled", *, error_stage: str = "wallet_provider") -> None:
        super().__init__(
            message,
            error_code="wallet_cancelled",
            error_stage=error_stage,
            retryable=False,
            status="cancelled",
        )


class WalletTimedOutError(WalletProviderError):
    def __init__(self, message: str = "wallet flow timed out", *, error_stage: str = "wallet_provider") -> None:
        super().__init__(
            message,
            error_code="wallet_timed_out",
            error_stage=error_stage,
            retryable=True,
            status="timed_out",
        )


class WalletUnknownResultError(WalletProviderError):
    def __init__(self, message: str, *, error_stage: str) -> None:
        super().__init__(
            message,
            error_code="wallet_result_unknown",
            error_stage=error_stage,
            retryable=False,
            status="unknown",
        )


def wallet_method_spec(payment_method: Any) -> WalletMethodSpec:
    key = str(payment_method or "").strip().lower().replace("-", "").replace("_", "")
    aliases = {"gopay": "gopay", "gcash": "gcash", "grabpay": "grabpay"}
    normalized = aliases.get(key, "")
    if not normalized:
        raise WalletProviderError(
            f"unsupported wallet payment method: {payment_method}",
            error_code="wallet_method_unsupported",
            error_stage="validation",
        )
    return WALLET_METHODS[normalized]


def redact_sensitive_text(value: Any) -> str:
    text = str(value or "")
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED_JWT]", text)
    text = _PROXY_AUTH_RE.sub(r"\1://[REDACTED]@", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    text = _STRIPE_SECRET_RE.sub("[REDACTED_STRIPE_KEY]", text)
    text = _SESSION_ID_RE.sub(lambda match: _identifier_hint(match.group(0)), text)
    return text


def build_payment_method_payload(
    contract: CheckoutRequestContract,
    checkout_session_id: str,
    publishable_key: str,
    identifiers: WalletFlowIdentifiers,
    *,
    billing_details: Mapping[str, Any] | None = None,
    time_on_page_ms: int = 30_000,
) -> dict[str, str]:
    billing = _billing_details(contract, billing_details)
    body = {
        "type": contract.stripe_payment_method,
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[address][country]": billing["country"],
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][state]": billing["state"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "payment_user_agent": (
            f"stripe.js/{identifiers.runtime_version}; stripe-js-v3/{identifiers.runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": str(max(0, int(time_on_page_ms))),
        "client_attribution_metadata[client_session_id]": identifiers.stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": checkout_session_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "key": publishable_key,
        "_stripe_version": contract.stripe_init_payload(publishable_key)["_stripe_version"],
    }
    return body


def build_confirm_payload(
    contract: CheckoutRequestContract,
    session: CheckoutSessionContract,
    init_payload: Mapping[str, Any],
    payment_method_id: str,
    identifiers: WalletFlowIdentifiers,
) -> dict[str, str]:
    evidence = StripeCapabilityEvidence.from_payload(init_payload, fallback_currency=contract.currency)
    hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    return_url = _confirm_return_url(hosted_url, session)
    return {
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "payment_method": payment_method_id,
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "version": identifiers.runtime_version,
        "expected_amount": str(evidence.amount_minor if evidence.amount_minor is not None else 0),
        "expected_payment_method_type": contract.stripe_payment_method,
        "return_url": return_url,
        "elements_session_client[session_id]": identifiers.elements_session_id,
        "elements_session_client[locale]": contract.payment_locale,
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": identifiers.stripe_js_id,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": identifiers.stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": session.checkout_session_id,
        "client_attribution_metadata[checkout_config_id]": str(
            init_payload.get("config_id") or identifiers.elements_session_config_id
        ),
        "client_attribution_metadata[elements_session_id]": identifiers.elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": identifiers.elements_session_config_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "key": session.publishable_key,
        "_stripe_version": contract.stripe_init_payload(session.publishable_key)["_stripe_version"],
    }


def build_payment_page_poll_payload(
    contract: CheckoutRequestContract,
    session: CheckoutSessionContract,
    identifiers: WalletFlowIdentifiers,
) -> dict[str, str]:
    return {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": identifiers.elements_session_id,
        "elements_session_client[stripe_js_id]": identifiers.stripe_js_id,
        "elements_session_client[locale]": contract.payment_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": session.publishable_key,
        "_stripe_version": contract.stripe_init_payload(session.publishable_key)["_stripe_version"],
    }


def capability_result(evidence: StripeCapabilityEvidence, contract: CheckoutRequestContract) -> dict[str, Any]:
    classification, supported = evidence.classification_for(contract.stripe_payment_method)
    return {
        "classification": classification,
        "conclusive": supported is not None,
        "supported": supported,
        "amount_minor": evidence.amount_minor,
        "currency": evidence.currency,
        "payment_method_types": list(evidence.payment_method_types),
        "ordered_payment_method_types": list(evidence.ordered_payment_method_types),
        "custom_payment_methods": list(evidence.custom_payment_methods),
        "offer_state": evidence.offer_state,
    }


def run_wallet_provider(
    payment_method: Any,
    access_token: str,
    transport: WalletProviderTransport,
    *,
    probe_only: bool = False,
    link_only: bool = False,
    billing_details: Mapping[str, Any] | None = None,
    auth_context: Mapping[str, Any] | None = None,
    transport_context: Mapping[str, Any] | None = None,
    resume_checkout: Mapping[str, Any] | None = None,
    stripe_publishable_key: str = "",
    checkout_country: str = "",
    currency: str = "",
    payment_locale: str = "",
    browser_locale: str = "",
    browser_timezone: str = "",
    promo_campaign_id: str = "plus-1-month-free",
    checkout_ui_mode: str = "custom",
    require_zero: bool = False,
    max_approve_attempts: int = 6,
    max_poll_attempts: int = 25,
    poll_interval_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    uuid_factory: Callable[[], Any] = uuid.uuid4,
) -> dict[str, Any]:
    """Probe or execute a wallet redirect flow through an injected transport."""

    stage = "validation"
    spec: WalletMethodSpec | None = None
    capability: dict[str, Any] | None = None
    flow_id = str(uuid_factory())
    context = dict(auth_context or {})
    wire_context = dict(transport_context or {})
    session: CheckoutSessionContract | None = None
    checkout_session_type = ""
    capability_source = ""
    checkout_ui_mode_used = ""
    initial_checkout_has_promo: bool | None = None
    promotion_applied: bool | None = None
    promotion_error = ""
    promotion_attempt_count = 0
    promotion_diagnostics: dict[str, Any] = {}
    checkout_page_checked = False
    checkout_page_amount: int | None = None
    checkout_page_error = ""
    chatgpt_checkout_url = ""
    custom_checkout = False
    checkout_eligibility: dict[str, Any] = {}
    checkout_response: Any = None
    resumed_checkout = False
    try:
        spec = wallet_method_spec(payment_method)
        token = str(access_token or "").strip()
        if not token:
            raise WalletProviderError(
                "access_token is required",
                error_code="wallet_access_token_missing",
                error_stage="validation",
            )
        contract = CheckoutRequestContract.for_payment_method(
            spec.key,
            billing_country=checkout_country,
            currency=currency,
            payment_locale=payment_locale,
            browser_locale=browser_locale,
            browser_timezone=browser_timezone,
            promo_campaign_id=promo_campaign_id,
            checkout_ui_mode=checkout_ui_mode,
        )
        checkout_ui_mode_used = contract.checkout_ui_mode
        identifiers = WalletFlowIdentifiers.create(uuid_factory)

        def _fresh_checkout() -> None:
            """Create a brand-new checkout session and capture eligibility."""
            nonlocal stage, checkout_response, session, chatgpt_checkout_url
            nonlocal custom_checkout, checkout_session_type, capability_source
            nonlocal initial_checkout_has_promo, checkout_eligibility
            stage = "checkout"
            checkout_payload = contract.checkout_payload()
            if spec.key == "gopay" and not _gopay_create_with_promo(wire_context):
                # Compatibility switch for the older GoPay flow: create a plain
                # checkout first, then attach the campaign via checkout/update.
                # The current working path keeps promo_campaign on the initial
                # checkout create request so Stripe does not snapshot full price.
                checkout_payload.pop("promo_campaign", None)
            initial_checkout_has_promo = bool(checkout_payload.get("promo_campaign"))
            checkout_response = transport.create_checkout(
                _request(
                    stage,
                    spec,
                    contract,
                    flow_id,
                    token,
                    checkout_payload,
                    auth_context=context,
                    transport_context=wire_context,
                )
            )
            checkout_eligibility = _checkout_eligibility_diagnostics(
                checkout_response,
                checkout_payload,
            )
            session = CheckoutSessionContract.from_payload(
                checkout_response,
                billing_country=contract.billing_country,
                fallback_publishable_key=stripe_publishable_key,
            )
            chatgpt_checkout_url = _checkout_url(session)
            custom_checkout = _is_custom_checkout_session(session.checkout_session_id)
            checkout_session_type = "custom" if custom_checkout else "stripe"
            capability_source = "checkout_response" if custom_checkout else "stripe_init"

        stage = "checkout"
        resume = _resume_checkout_session(resume_checkout)
        if resume:
            # Reuse an existing zero-due checkout session instead of creating a
            # new one: OpenAI grants the plus-1-month-free campaign only to the
            # first checkout(s) of an account, so re-creating a checkout on
            # every retry burns the trial and leaves later attempts full price.
            try:
                session = CheckoutSessionContract.from_payload(
                    resume,
                    billing_country=contract.billing_country,
                    fallback_publishable_key=stripe_publishable_key,
                )
                chatgpt_checkout_url = _checkout_url(session)
                custom_checkout = _is_custom_checkout_session(session.checkout_session_id)
                checkout_session_type = "custom" if custom_checkout else "stripe"
                capability_source = "checkout_response" if custom_checkout else "stripe_init"
                initial_checkout_has_promo = True
                resumed_checkout = True
            except Exception:
                # The persisted session is unusable; fall through to a fresh one.
                resumed_checkout = False
        if not resumed_checkout:
            _fresh_checkout()

        promotion_response: Mapping[str, Any] | None = None
        update_promotion = getattr(transport, "update_checkout_promotion", None)
        promotion_payload = {
            "checkout_session_id": session.checkout_session_id,
            "processor_entity": session.processor_entity,
            "plan_name": contract.plan_name,
            "price_interval": "month",
            "seat_quantity": 1,
            "promo_campaign": {
                "promo_campaign_id": contract.promo_campaign_id,
                "is_coupon_from_query_param": False,
            },
        }
        promotion_attempt_limit = _promotion_attempt_limit(wire_context)

        def apply_promotion(attempt: int) -> None:
            """Apply the campaign to this checkout session and retain its evidence."""
            nonlocal promotion_response, promotion_applied, promotion_error
            nonlocal promotion_attempt_count, promotion_diagnostics, stage
            stage = "promotion"
            promotion_attempt_count += 1
            try:
                response = update_promotion(
                    _request(
                        stage,
                        spec,
                        contract,
                        flow_id,
                        token,
                        promotion_payload,
                        attempt=attempt,
                        session=session,
                        auth_context=context,
                        transport_context=wire_context,
                    )
                )
                promotion_response = response if isinstance(response, Mapping) else None
                promotion_applied = not (
                    isinstance(response, Mapping)
                    and response.get("success") is False
                )
                promotion_error = "" if promotion_applied else "promotion_rejected"
                promotion_diagnostics = _promotion_response_diagnostics(
                    response,
                    contract=contract,
                    session=session,
                    transport_context=wire_context,
                    attempt=attempt,
                    request_payload=promotion_payload,
                )
            except Exception as exc:
                promotion_response = None
                promotion_applied = False
                promotion_error = _promotion_error_text(exc)
                promotion_diagnostics = _promotion_exception_diagnostics(
                    exc,
                    session=session,
                    transport_context=wire_context,
                    attempt=attempt,
                )

        if resumed_checkout:
            # The resumed session already carries the campaign from its
            # original run; do not re-apply it (the update endpoint can also
            # be Cloudflare-blocked and waste the attempt budget).
            promotion_applied = True
            promotion_error = ""
        elif (
            promo_campaign_id
            and callable(update_promotion)
            and not _gopay_skip_promotion_update(wire_context, bool(initial_checkout_has_promo))
        ):
            # Live evidence (2026-08): sending promo_campaign in the initial
            # checkout create already attaches the 100% coupon; a follow-up
            # checkout/update re-apply can leave Stripe serving the full-price
            # snapshot instead (promotion_not_reflected_in_amount). Only run the
            # update for the compatibility flow that creates a plain checkout.
            apply_promotion(1)
        elif initial_checkout_has_promo:
            # The create carried the campaign and the update is skipped; the
            # coupon is already attached server-side, so treat it as applied.
            promotion_applied = True
            promotion_error = ""

        def read_amount_evidence() -> tuple[Mapping[str, Any], StripeCapabilityEvidence]:
            nonlocal stage
            if custom_checkout:
                payload = _merge_latest_checkout_evidence(
                    _checkout_evidence_payload(session),
                    promotion_response,
                )
            else:
                stage = "stripe_init"
                init_request_payload = contract.stripe_init_payload(
                    session.publishable_key,
                    stripe_js_id=identifiers.stripe_js_id,
                )
                payload = transport.stripe_init(
                    _request(
                        stage,
                        spec,
                        contract,
                        flow_id,
                        token,
                        init_request_payload,
                        session=session,
                        auth_context=context,
                        transport_context=wire_context,
                    )
                )
            return payload, StripeCapabilityEvidence.from_payload(
                payload,
                fallback_currency=contract.currency,
            )

        try:
            init_payload, evidence = read_amount_evidence()
        except Exception as resume_exc:
            if not resumed_checkout:
                raise
            # A resumed checkout session can expire or be closed server-side;
            # rebuild a fresh checkout (which may also be the only way to
            # recover a session that was revoked between runs).
            checkout_eligibility = {}
            _fresh_checkout()
            promotion_applied = True
            promotion_attempt_count = 0
            init_payload, evidence = read_amount_evidence()
        # A successful update response can still leave Stripe serving the
        # previous amount snapshot. Re-apply the campaign and re-read the
        # same checkout session a small number of times before rejecting it.
        if (
            spec.key == "gopay"
            and link_only
            and require_zero
            and callable(update_promotion)
            and not _gopay_skip_promotion_update(wire_context, bool(initial_checkout_has_promo))
            and evidence.amount_minor not in (None, 0)
        ):
            for promotion_attempt in range(2, promotion_attempt_limit + 1):
                apply_promotion(promotion_attempt)
                init_payload, evidence = read_amount_evidence()
                if evidence.amount_minor in (None, 0):
                    break
        # Stripe init can briefly serve the pre-promotion amount while the
        # hosted checkout page already reflects the updated session state.
        # Reconcile both custom and Stripe sessions before enforcing the
        # zero-due gate, while keeping the same checkout session throughout.
        if (
            link_only
            and require_zero
            and evidence.amount_minor not in (None, 0)
        ):
            read_checkout_page = getattr(transport, "read_checkout_page", None)
            if callable(read_checkout_page):
                checkout_page_checked = True
                try:
                    page_response = read_checkout_page(
                        _request(
                            "checkout_page",
                            spec,
                            contract,
                            flow_id,
                            token,
                            {},
                            session=session,
                            auth_context=context,
                            transport_context=wire_context,
                        )
                    )
                    page_amount, page_currency = extract_amount_from_checkout_html(
                        page_response.get("html")
                        if isinstance(page_response, Mapping)
                        else ""
                    )
                    checkout_page_amount = page_amount
                    if page_amount is not None:
                        page_payload: dict[str, Any] = {
                            "total_summary": {"due": page_amount},
                        }
                        if page_currency:
                            page_payload["total_summary"]["currency"] = page_currency
                        init_payload = _merge_evidence_payload(init_payload, page_payload)
                        evidence = StripeCapabilityEvidence.from_payload(
                            init_payload,
                            fallback_currency=contract.currency,
                        )
                        capability_source = "checkout_page"
                        if evidence.amount_minor == 0:
                            capability_source = "checkout_page"
                    else:
                        checkout_page_error = "checkout_page_amount_missing"
                except Exception as exc:
                    checkout_page_error = _promotion_error_text(exc)
        if (
            promotion_applied is True
            and evidence.amount_minor is not None
            and evidence.amount_minor != 0
        ):
            promotion_applied = False
            promotion_error = "promotion_not_reflected_in_amount"
        capability = capability_result(evidence, contract)
        base_result = {
            "payment_method": spec.key,
            "capability": capability,
            "checkout_session_id_hint": _identifier_hint(session.checkout_session_id),
            "checkout_session_type": "custom" if custom_checkout else "stripe",
            "capability_source": capability_source,
            "promotion_applied": promotion_applied,
            "promotion_error": promotion_error,
            "promotion_attempt_count": promotion_attempt_count,
            "checkout_page_checked": checkout_page_checked,
            "checkout_page_amount_minor": checkout_page_amount,
            "checkout_page_error": checkout_page_error,
            "retryable": False,
            "error_stage": "",
            "chatgpt_checkout_url": chatgpt_checkout_url,
            "checkout_ui_mode": contract.checkout_ui_mode,
            "initial_checkout_has_promo": bool(initial_checkout_has_promo),
            "checkout_session_id": session.checkout_session_id,
            "processor_entity": session.processor_entity,
            "publishable_key": session.publishable_key,
        }
        if checkout_eligibility:
            base_result["checkout_eligibility"] = checkout_eligibility
        if promotion_diagnostics:
            base_result["promotion_diagnostics"] = promotion_diagnostics

        # Probe mode must use the same promotion and zero-due evidence path,
        # but it must stop before creating a Stripe payment method.
        if probe_only:
            if capability["supported"] is False:
                raise WalletProviderError(
                    f"checkout does not offer {spec.label}: payment_method_types={capability['payment_method_types']}",
                    error_code="wallet_method_unavailable",
                    error_stage=capability_source,
                )
            if require_zero and evidence.amount_minor is not None and evidence.amount_minor != 0:
                raise WalletProviderError(
                    _not_zero_due_message(evidence.amount_minor, evidence.currency, checkout_eligibility),
                    error_code="wallet_checkout_not_zero_due",
                    error_stage=capability_source,
                )
            return {
                **base_result,
                "ok": True,
                "status": "probe_complete",
                "operation": "probe",
                "probe_only": True,
                "url": "",
            }

        # A link-only request still performs capability and price checks.
        # Most methods can stop after Checkout/Stripe init; GoPay needs a
        # Stripe authorization handoff URL, so it may create a payment method
        # and confirm the page to obtain pm-redirects without opening Checkout.
        if link_only:
            if capability["supported"] is False:
                raise WalletProviderError(
                    f"checkout does not offer {spec.label}: payment_method_types={capability['payment_method_types']}",
                    error_code="wallet_method_unavailable",
                    error_stage=capability_source,
                )
            if require_zero and evidence.amount_minor is not None and evidence.amount_minor != 0:
                raise WalletProviderError(
                    _not_zero_due_message(evidence.amount_minor, evidence.currency, checkout_eligibility),
                    error_code="wallet_checkout_not_zero_due",
                    error_stage=capability_source,
                )
            checkout_url = _checkout_url(session)
            if spec.key == "gopay":
                checkout_url = str(init_payload.get("stripe_hosted_url") or checkout_url).strip()
            if not checkout_url:
                raise WalletProviderError(
                    "checkout response did not produce a usable checkout URL",
                    error_code="checkout_link_missing",
                    error_stage="checkout_response",
                    retryable=True,
                )
            if spec.key == "gopay":
                direct_error = ""
                resolved_billing = _billing_details(contract, billing_details)
                if not custom_checkout:
                    try:
                        stage = "payment_method"
                        payment_method_payload = build_payment_method_payload(
                            contract,
                            session.checkout_session_id,
                            session.publishable_key,
                            identifiers,
                            billing_details=resolved_billing,
                        )
                        payment_method_response = transport.create_payment_method(
                            _request(
                                stage,
                                spec,
                                contract,
                                flow_id,
                                token,
                                payment_method_payload,
                                session=session,
                                auth_context=context,
                                transport_context=wire_context,
                            )
                        )
                        payment_method_id = _payment_method_id(payment_method_response)

                        stage = "confirm"
                        confirm_payload = build_confirm_payload(
                            contract,
                            session,
                            init_payload,
                            payment_method_id,
                            identifiers,
                        )
                        confirm_response = transport.confirm_payment(
                            _request(
                                stage,
                                spec,
                                contract,
                                flow_id,
                                token,
                                confirm_payload,
                                session=session,
                                auth_context=context,
                                transport_context=wire_context,
                            )
                        )
                        _raise_for_terminal_payload(confirm_response, stage)
                        # Stripe may place the handoff inside a nested JSON
                        # field, an escaped JSON string, or an HTML response
                        # fragment. Search for the provider URL itself before
                        # considering generic redirect fields.
                        direct_url = _extract_provider_redirect_url(confirm_response, spec)
                        if _is_provider_url(direct_url, spec):
                            return {
                                **base_result,
                                "ok": True,
                                "status": "completed",
                                "operation": "extract_link",
                                "probe_only": False,
                                "url": direct_url,
                                "provider_redirect_url": direct_url,
                                "checkout_url": checkout_url,
                                "cs_id": session.checkout_session_id,
                                "processor_entity": session.processor_entity,
                                "link_type": "gopay_qr_authorize_link",
                            }
                        direct_error = (
                            "Stripe confirm did not return a GoPay QR authorization URL; "
                            + _confirm_response_diagnostics(confirm_response)
                        )
                        # GoPay commonly materializes the pm-redirects handoff
                        # only after ChatGPT approves the checkout and Stripe
                        # refreshes the payment page. This is still link-only:
                        # return the intermediate provider URL and do not follow
                        # it or submit any provider payment.
                        try:
                            stage = "approve"
                            approve_payload = {
                                "checkout_session_id": session.checkout_session_id,
                                "processor_entity": session.processor_entity,
                            }
                            approved = False
                            last_approval_response: Mapping[str, Any] | None = None
                            for attempt in range(1, max(1, int(max_approve_attempts)) + 1):
                                approval_response = transport.approve_checkout(
                                    _request(
                                        stage,
                                        spec,
                                        contract,
                                        flow_id,
                                        token,
                                        approve_payload,
                                        attempt=attempt,
                                        session=session,
                                        auth_context=context,
                                        transport_context=wire_context,
                                    )
                                )
                                last_approval_response = approval_response
                                approval_url = _extract_provider_redirect_url(approval_response, spec)
                                if _is_provider_url(approval_url, spec):
                                    return {
                                        **base_result,
                                        "ok": True,
                                        "status": "completed",
                                        "operation": "extract_link",
                                        "probe_only": False,
                                        "url": approval_url,
                                        "provider_redirect_url": approval_url,
                                        "checkout_url": checkout_url,
                                        "cs_id": session.checkout_session_id,
                                        "processor_entity": session.processor_entity,
                                        "link_type": "gopay_qr_authorize_link",
                                    }
                                approval_state = _response_state(approval_response, prefer_result=True)
                                if approval_state == "approved":
                                    approved = True
                                    break
                                _raise_for_terminal_payload(approval_response, stage)
                                if attempt < max_approve_attempts:
                                    sleep(max(0.0, float(poll_interval_seconds)))

                            if not approved:
                                direct_error += (
                                    "; ChatGPT approve did not become approved; "
                                    f"{_confirm_response_diagnostics(last_approval_response)}"
                                )
                            else:
                                stage = "poll"
                                poll_payload = build_payment_page_poll_payload(contract, session, identifiers)
                                poll_url = ""
                                last_poll_response: Mapping[str, Any] | None = None
                                for attempt in range(1, max(1, int(max_poll_attempts)) + 1):
                                    poll_response = transport.poll_payment(
                                        _request(
                                            stage,
                                            spec,
                                            contract,
                                            flow_id,
                                            token,
                                            poll_payload,
                                            attempt=attempt,
                                            session=session,
                                            auth_context=context,
                                            transport_context=wire_context,
                                        )
                                    )
                                    last_poll_response = poll_response
                                    _raise_for_terminal_payload(poll_response, stage)
                                    poll_url = _extract_provider_redirect_url(poll_response, spec)
                                    if not _is_provider_url(poll_url, spec):
                                        extract_intent_redirect = getattr(transport, "extract_intent_redirect", None)
                                        if callable(extract_intent_redirect):
                                            intent_response = extract_intent_redirect(
                                                _request(
                                                    stage,
                                                    spec,
                                                    contract,
                                                    flow_id,
                                                    token,
                                                    {"payment_page_response": poll_response},
                                                    attempt=attempt,
                                                    session=session,
                                                    auth_context=context,
                                                    transport_context=wire_context,
                                                )
                                            )
                                            poll_url = _extract_provider_redirect_url(intent_response, spec)
                                    if _is_provider_url(poll_url, spec):
                                        return {
                                            **base_result,
                                            "ok": True,
                                            "status": "completed",
                                            "operation": "extract_link",
                                            "probe_only": False,
                                            "url": poll_url,
                                            "provider_redirect_url": poll_url,
                                            "checkout_url": checkout_url,
                                            "cs_id": session.checkout_session_id,
                                            "processor_entity": session.processor_entity,
                                            "link_type": "gopay_qr_authorize_link",
                                        }
                                    if attempt < max_poll_attempts:
                                        sleep(max(0.0, float(poll_interval_seconds)))
                                direct_error += (
                                    f"; payment_pages poll did not return a GoPay QR URL "
                                    f"after {max(1, int(max_poll_attempts))} attempt(s); "
                                    f"{_confirm_response_diagnostics(last_poll_response)}"
                                )
                        except Exception as exc:
                            direct_error += f"; approve/poll failed: {redact_sensitive_text(exc)}"
                    except Exception as exc:
                        direct_error = redact_sensitive_text(exc)
                if not _as_bool(wire_context.get("gopay_browser_fallback")):
                    raise WalletProviderError(
                        "GoPay protocol confirm did not return a pm-redirects/Midtrans authorization URL"
                        + (f": {direct_error}" if direct_error else ""),
                        error_code="gopay_authorize_url_missing",
                        error_stage="confirm",
                        retryable=True,
                    )
                extract_provider_link = getattr(transport, "extract_provider_link", None)
                if not callable(extract_provider_link):
                    raise WalletProviderError(
                        "GoPay transport does not support Midtrans QR-link extraction",
                        error_code="gopay_browser_extractor_unavailable",
                        error_stage="extract_link",
                        retryable=True,
                    )
                stage = "extract_link"
                provider_response = extract_provider_link(
                    _request(
                        stage,
                        spec,
                        contract,
                        flow_id,
                        token,
                        {
                            "checkout_url": checkout_url,
                            "billing_details": resolved_billing,
                            "direct_error": direct_error,
                        },
                        session=session,
                        auth_context=context,
                        transport_context=wire_context,
                        redirect_url=checkout_url,
                    )
                )
                provider_url = _followed_url(provider_response)
                if not _is_provider_url(provider_url, spec):
                    raise WalletProviderError(
                        "GoPay browser flow did not return a QR authorization URL",
                        error_code="gopay_qr_authorize_url_missing",
                        error_stage=stage,
                        retryable=True,
                    )
                return {
                    **base_result,
                    "ok": True,
                    "status": "completed",
                    "operation": "extract_link",
                    "probe_only": False,
                    "url": provider_url,
                    "provider_redirect_url": provider_url,
                    "checkout_url": checkout_url,
                    "cs_id": session.checkout_session_id,
                    "processor_entity": session.processor_entity,
                    "link_type": "gopay_qr_authorize_link",
                }
            return {
                **base_result,
                "ok": True,
                "status": "completed",
                "operation": "extract_link",
                "probe_only": False,
                "url": checkout_url,
                "checkout_url": checkout_url,
                "cs_id": session.checkout_session_id,
                "processor_entity": session.processor_entity,
                "link_type": f"{spec.key}_checkout_link",
            }

        if capability["supported"] is False:
            raise WalletProviderError(
                f"checkout does not offer {spec.label}: payment_method_types={capability['payment_method_types']}",
                error_code="wallet_method_unavailable",
                error_stage=capability_source,
            )
        if capability["supported"] is None:
            raise WalletProviderError(
                f"checkout did not provide conclusive {spec.label} capability evidence",
                error_code="wallet_capability_unknown",
                error_stage=capability_source,
                retryable=False,
                status="unknown",
            )
        if require_zero and evidence.amount_minor != 0:
            raise WalletProviderError(
                _not_zero_due_message(evidence.amount_minor, evidence.currency, checkout_eligibility),
                error_code="wallet_checkout_not_zero_due",
                error_stage=capability_source,
            )
        if custom_checkout:
            raise WalletProviderError(
                "custom ChatGPT checkout sessions cannot be executed through Stripe payment_pages",
                error_code="wallet_custom_checkout_execution_unsupported",
                error_stage="checkout_response",
            )

        stage = "payment_method"
        payment_method_payload = build_payment_method_payload(
            contract,
            session.checkout_session_id,
            session.publishable_key,
            identifiers,
            billing_details=billing_details,
        )
        payment_method_response = transport.create_payment_method(
            _request(
                stage,
                spec,
                contract,
                flow_id,
                token,
                payment_method_payload,
                session=session,
                auth_context=context,
                transport_context=wire_context,
            )
        )
        payment_method_id = _payment_method_id(payment_method_response)

        stage = "confirm"
        confirm_payload = build_confirm_payload(contract, session, init_payload, payment_method_id, identifiers)
        confirm_response = transport.confirm_payment(
            _request(
                stage,
                spec,
                contract,
                flow_id,
                token,
                confirm_payload,
                session=session,
                auth_context=context,
                transport_context=wire_context,
            )
        )
        _raise_for_terminal_payload(confirm_response, stage)
        redirect_url = _extract_redirect_url(confirm_response)

        stage = "approve"
        approve_payload = {
            "checkout_session_id": session.checkout_session_id,
            "processor_entity": session.processor_entity,
        }
        approved = False
        for attempt in range(1, max(1, int(max_approve_attempts)) + 1):
            approval_response = transport.approve_checkout(
                _request(
                    stage,
                    spec,
                    contract,
                    flow_id,
                    token,
                    approve_payload,
                    attempt=attempt,
                    session=session,
                    auth_context=context,
                    transport_context=wire_context,
                )
            )
            approval_state = _response_state(approval_response, prefer_result=True)
            if approval_state == "approved":
                approved = True
                redirect_url = redirect_url or _extract_redirect_url(approval_response)
                break
            _raise_for_terminal_payload(approval_response, stage)
            if attempt < max_approve_attempts:
                sleep(max(0.0, float(poll_interval_seconds)))
        if not approved:
            raise WalletUnknownResultError(
                "ChatGPT checkout approval did not become approved",
                error_stage=stage,
            )

        stage = "poll"
        poll_payload = build_payment_page_poll_payload(contract, session, identifiers)
        for attempt in range(1, max(1, int(max_poll_attempts)) + 1):
            if redirect_url:
                break
            poll_response = transport.poll_payment(
                _request(
                    stage,
                    spec,
                    contract,
                    flow_id,
                    token,
                    poll_payload,
                    attempt=attempt,
                    session=session,
                    auth_context=context,
                    transport_context=wire_context,
                )
            )
            _raise_for_terminal_payload(poll_response, stage)
            redirect_url = _extract_redirect_url(poll_response)
            if not redirect_url:
                extract_intent_redirect = getattr(transport, "extract_intent_redirect", None)
                if callable(extract_intent_redirect):
                    intent_response = extract_intent_redirect(
                        _request(
                            stage,
                            spec,
                            contract,
                            flow_id,
                            token,
                            {"payment_page_response": poll_response},
                            attempt=attempt,
                            session=session,
                            auth_context=context,
                            transport_context=wire_context,
                        )
                    )
                    redirect_url = _extract_redirect_url(intent_response)
            if not redirect_url and attempt < max_poll_attempts:
                sleep(max(0.0, float(poll_interval_seconds)))
        if not redirect_url:
            raise WalletUnknownResultError(
                "wallet redirect did not materialize before polling ended",
                error_stage=stage,
            )

        stage = "follow_redirect"
        follow_response = transport.follow_redirect(
            _request(
                stage,
                spec,
                contract,
                flow_id,
                token,
                {},
                session=session,
                auth_context=context,
                transport_context=wire_context,
                redirect_url=redirect_url,
            )
        )
        provider_url = _followed_url(follow_response) or redirect_url
        if not _is_provider_url(provider_url, spec):
            raise WalletUnknownResultError(
                f"redirect chain did not resolve to a recognized {spec.label} provider host",
                error_stage=stage,
            )
        return {
            **base_result,
            "ok": True,
            "status": "completed",
            "operation": "extract_link",
            "probe_only": False,
            "url": provider_url,
            "provider_redirect_url": provider_url,
            "link_type": f"{spec.key}_protocol",
        }
    except asyncio.CancelledError:
        return _failure_result(
            spec,
            WalletCancelledError(error_stage=stage),
            capability=capability,
            checkout_session_type=checkout_session_type,
            capability_source=capability_source,
            promotion_applied=promotion_applied,
            promotion_error=promotion_error,
            promotion_attempt_count=promotion_attempt_count,
            promotion_diagnostics=promotion_diagnostics,
            checkout_ui_mode=checkout_ui_mode_used,
            initial_checkout_has_promo=initial_checkout_has_promo,
            checkout_page_checked=checkout_page_checked,
            checkout_page_amount=checkout_page_amount,
            checkout_page_error=checkout_page_error,
            checkout_session_id=session.checkout_session_id if session else "",
            checkout_url=chatgpt_checkout_url,
            checkout_eligibility=checkout_eligibility,
        )
    except concurrent.futures.CancelledError:
        return _failure_result(
            spec,
            WalletCancelledError(error_stage=stage),
            capability=capability,
            checkout_session_type=checkout_session_type,
            capability_source=capability_source,
            promotion_applied=promotion_applied,
            promotion_error=promotion_error,
            promotion_attempt_count=promotion_attempt_count,
            promotion_diagnostics=promotion_diagnostics,
            checkout_ui_mode=checkout_ui_mode_used,
            initial_checkout_has_promo=initial_checkout_has_promo,
            checkout_page_checked=checkout_page_checked,
            checkout_page_amount=checkout_page_amount,
            checkout_page_error=checkout_page_error,
            checkout_session_id=session.checkout_session_id if session else "",
            checkout_url=chatgpt_checkout_url,
            checkout_eligibility=checkout_eligibility,
        )
    except Exception as exc:
        return _failure_result(
            spec,
            _structured_error(exc, stage),
            capability=capability,
            checkout_session_type=checkout_session_type,
            capability_source=capability_source,
            promotion_applied=promotion_applied,
            promotion_error=promotion_error,
            promotion_attempt_count=promotion_attempt_count,
            promotion_diagnostics=promotion_diagnostics,
            checkout_ui_mode=checkout_ui_mode_used,
            initial_checkout_has_promo=initial_checkout_has_promo,
            checkout_page_checked=checkout_page_checked,
            checkout_page_amount=checkout_page_amount,
            checkout_page_error=checkout_page_error,
            checkout_session_id=session.checkout_session_id if session else "",
            checkout_url=chatgpt_checkout_url,
            checkout_eligibility=checkout_eligibility,
        )


def _request(
    stage: str,
    spec: WalletMethodSpec,
    contract: CheckoutRequestContract,
    flow_id: str,
    access_token: str,
    payload: Mapping[str, Any],
    *,
    attempt: int = 1,
    session: CheckoutSessionContract | None = None,
    auth_context: Mapping[str, Any],
    transport_context: Mapping[str, Any],
    redirect_url: str = "",
) -> WalletTransportRequest:
    return WalletTransportRequest(
        stage=stage,
        method=spec.key,
        contract=contract,
        flow_id=flow_id,
        attempt=attempt,
        checkout_session_id=session.checkout_session_id if session else "",
        processor_entity=session.processor_entity if session else "",
        access_token=access_token,
        publishable_key=session.publishable_key if session else "",
        payload=payload,
        auth_context=auth_context,
        transport_context=transport_context,
        redirect_url=redirect_url,
    )


def _billing_details(
    contract: CheckoutRequestContract,
    supplied: Mapping[str, Any] | None,
) -> dict[str, str]:
    defaults = random_billing_details(contract.billing_country)
    merged = {**defaults, **{str(k): str(v) for k, v in dict(supplied or {}).items() if v is not None}}
    merged["country"] = contract.billing_country
    required = ("name", "email", "line1", "city", "state", "postal_code")
    missing = [key for key in required if not str(merged.get(key) or "").strip()]
    if missing:
        raise WalletProviderError(
            f"billing_details missing required fields: {', '.join(missing)}",
            error_code="wallet_billing_invalid",
            error_stage="payment_method",
        )
    return {key: str(value).strip() for key, value in merged.items()}


_RANDOM = random.SystemRandom()

_BILLING_NAMES_ID = (
    "Budi Santoso", "Dewi Lestari", "Agus Pratama", "Siti Rahma",
    "Rizky Maulana", "Maya Putri", "Arif Wibowo", "Nadia Permata",
)
_BILLING_STREETS_ID = (
    "Jl. Jend. Sudirman", "Jl. M. H. Thamrin", "Jl. Gatot Subroto",
    "Jl. Asia Afrika", "Jl. Diponegoro", "Jl. Ahmad Yani",
    "Jl. Pemuda", "Jl. Merdeka",
)
_BILLING_CITIES_ID = (
    ("Jakarta Pusat", "DKI Jakarta", "10220"),
    ("Jakarta Selatan", "DKI Jakarta", "12190"),
    ("Bandung", "Jawa Barat", "40115"),
    ("Surabaya", "Jawa Timur", "60271"),
    ("Denpasar", "Bali", "80234"),
    ("Yogyakarta", "DI Yogyakarta", "55281"),
    ("Medan", "Sumatera Utara", "20111"),
    ("Makassar", "Sulawesi Selatan", "90111"),
    ("Semarang", "Jawa Tengah", "50132"),
    ("Tangerang", "Banten", "15111"),
)
_BILLING_NAMES_PH = (
    "Miguel Santos", "Maria Reyes", "Jose Garcia", "Ana Cruz",
    "Carlo Mendoza", "Sofia Ramos",
)
_BILLING_STREETS_PH = (
    "Ayala Avenue", "Rizal Avenue", "Makati Avenue", "Ortigas Avenue",
    "Taft Avenue", "Quezon Avenue",
)
_BILLING_CITIES_PH = (
    ("Makati", "Metro Manila", "1226"),
    ("Manila", "Metro Manila", "1000"),
    ("Quezon City", "Metro Manila", "1100"),
    ("Cebu City", "Cebu", "6000"),
)


def random_billing_details(country: Any) -> dict[str, str]:
    country_code = str(country or "").strip().upper()
    if country_code == "ID":
        city, state, postal = _RANDOM.choice(_BILLING_CITIES_ID)
        return {
            "name": _RANDOM.choice(_BILLING_NAMES_ID),
            "email": "buyer@example.com",
            "line1": f"{_RANDOM.choice(_BILLING_STREETS_ID)} No. {_RANDOM.randint(1, 199)}",
            "city": city,
            "state": state,
            "postal_code": postal,
            "country": "ID",
        }
    if country_code == "PH":
        city, state, postal = _RANDOM.choice(_BILLING_CITIES_PH)
        return {
            "name": _RANDOM.choice(_BILLING_NAMES_PH),
            "email": "buyer@example.com",
            "line1": f"{_RANDOM.randint(1, 9999)} {_RANDOM.choice(_BILLING_STREETS_PH)}",
            "city": city,
            "state": state,
            "postal_code": postal,
            "country": "PH",
        }
    return {}


def _payment_method_id(response: Mapping[str, Any] | str) -> str:
    value = response if isinstance(response, str) else response.get("id") if isinstance(response, Mapping) else ""
    payment_method_id = str(value or "").strip()
    if not payment_method_id.startswith("pm_"):
        raise WalletProviderError(
            "Stripe payment method response did not contain a payment method id",
            error_code="wallet_payment_method_bad_response",
            error_stage="payment_method",
            retryable=True,
        )
    return payment_method_id


def _confirm_return_url(hosted_url: str, session: CheckoutSessionContract) -> str:
    if hosted_url.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + hosted_url[len("https://checkout.stripe.com") :]
    if hosted_url.startswith("https://pay.openai.com"):
        return hosted_url
    return (
        f"https://pay.openai.com/c/pay/{session.checkout_session_id}"
        "?returned_from_redirect=true&ui_mode=custom"
    )


def _response_state(payload: Any, *, prefer_result: bool = False) -> str:
    if not isinstance(payload, Mapping):
        return ""
    keys = ("result", "state", "status") if prefer_result else ("state", "status", "result")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _raise_for_terminal_payload(payload: Any, stage: str) -> None:
    state = _response_state(payload)
    if state in _TERMINAL_CANCEL_STATES:
        raise WalletCancelledError(error_stage=stage)
    if state in _TERMINAL_FAILURE_STATES:
        raise WalletProviderError(
            f"wallet provider returned terminal state: {state}",
            error_code="wallet_provider_rejected",
            error_stage=stage,
        )
    if isinstance(payload, Mapping) and payload.get("error"):
        error = payload.get("error")
        message = error.get("message") if isinstance(error, Mapping) else error
        raise WalletProviderError(
            f"wallet provider error: {message or 'unknown error'}",
            error_code="wallet_provider_response_error",
            error_stage=stage,
            retryable=stage in _SIDE_EFFECT_STAGES,
            status="unknown" if stage in _SIDE_EFFECT_STAGES else "failed",
        )


def _extract_redirect_url(payload: Any, *, depth: int = 0) -> str:
    if depth > 10:
        return ""
    if isinstance(payload, str):
        return payload.strip() if _is_http_url(payload) else ""
    if isinstance(payload, Mapping):
        for key in ("provider_redirect_url", "final_url", "redirect_url", "url"):
            value = payload.get(key)
            if isinstance(value, str) and _is_http_url(value):
                return value.strip()
        for key, value in payload.items():
            if str(key).lower() in {"return_url", "success_url", "cancel_url", "stripe_hosted_url"}:
                continue
            found = _extract_redirect_url(value, depth=depth + 1)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found = _extract_redirect_url(value, depth=depth + 1)
            if found:
                return found
    return ""


def _extract_provider_redirect_url(payload: Any, spec: WalletMethodSpec, *, depth: int = 0) -> str:
    """Find a wallet handoff URL embedded anywhere in a response payload."""
    if depth > 12:
        return ""
    if isinstance(payload, Mapping):
        preferred = (
            "provider_redirect_url",
            "redirect_url",
            "url",
            "final_url",
            "location",
            "body",
            "html",
            "data",
            "next_action",
        )
        values = [payload.get(key) for key in preferred if key in payload]
        values.extend(value for key, value in payload.items() if key not in preferred)
    elif isinstance(payload, (list, tuple)):
        values = list(payload)
    else:
        values = [payload]

    for value in values:
        text = _normalize_provider_capture_text(value)
        if text:
            for pattern in (_GOPAY_PM_REDIRECT_RE, _GOPAY_MIDTRANS_RE):
                match = pattern.search(text)
                if match and _is_provider_url(match.group(0), spec):
                    return match.group(0).rstrip(".,;)]}")
        if isinstance(value, (Mapping, list, tuple)):
            found = _extract_provider_redirect_url(value, spec, depth=depth + 1)
            if found:
                return found
    return ""


def _normalize_provider_capture_text(value: Any) -> str:
    text = html.unescape(str(value or "").strip())
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return (
        text.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u002F", "/")
        .replace("&amp;", "&")
    )


def _confirm_response_diagnostics(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return f"response_type={type(payload).__name__}"
    top_keys = [str(key) for key in payload.keys()][:20]
    nested: dict[str, Any] = {}
    for key in (
        "next_action",
        "setup_intent",
        "payment_intent",
        "submission_attempt",
        "error",
        "status",
        "state",
        "result",
        "type",
        "code",
    ):
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, Mapping):
            nested[key] = [str(child) for child in value.keys()][:15]
        elif isinstance(value, (str, int, float, bool)):
            nested[key] = redact_sensitive_text(str(value))[:120]
        else:
            nested[key] = type(value).__name__
    submission = payload.get("submission_attempt")
    if isinstance(submission, Mapping):
        submission_markers: dict[str, Any] = {}
        for key in ("state", "status", "code", "reason", "message", "error"):
            value = submission.get(key)
            if isinstance(value, Mapping):
                submission_markers[key] = {
                    str(child): redact_sensitive_text(str(value.get(child) or ""))[:100]
                    for child in ("code", "message", "type")
                    if value.get(child) not in (None, "")
                }
            elif value not in (None, ""):
                submission_markers[key] = redact_sensitive_text(str(value))[:120]
        if submission_markers:
            nested["submission_markers"] = submission_markers
    return f"response_keys={top_keys}; markers={nested}"


def _followed_url(response: Mapping[str, Any] | str) -> str:
    return _extract_redirect_url(response)


def _is_http_url(value: Any) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return False
    return parsed.scheme.lower() == "https" and bool(parsed.hostname)


def _is_provider_url(value: str, spec: WalletMethodSpec) -> bool:
    if not _is_http_url(value):
        return False
    host = str(urlsplit(value).hostname or "").lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in spec.redirect_hosts)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_custom_checkout_session(session_id: Any) -> bool:
    return str(session_id or "").strip().startswith("oaics_")


def _checkout_url(session: CheckoutSessionContract) -> str:
    session_id = str(session.checkout_session_id or "").strip()
    processor_entity = str(session.processor_entity or "").strip()
    if not session_id or not processor_entity:
        return ""
    return f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"


def _checkout_evidence_payload(session: CheckoutSessionContract) -> Mapping[str, Any]:
    payload = getattr(session, "raw_payload", None)
    if isinstance(payload, Mapping):
        return payload
    raise WalletProviderError(
        "custom checkout response did not include a payment-method catalog",
        error_code="wallet_capability_unknown",
        error_stage="checkout_response",
        retryable=False,
        status="unknown",
    )


def _merge_evidence_payload(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Overlay checkout/update evidence without losing nested checkout data."""
    if not isinstance(overlay, Mapping):
        return base
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        previous = merged.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_evidence_payload(previous, value)
        else:
            merged[key] = value
    return merged


def _merge_latest_checkout_evidence(
    base: Mapping[str, Any],
    latest: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Merge checkout/update data while giving the latest due amount priority."""
    merged = _merge_evidence_payload(base, latest)
    if not isinstance(latest, Mapping):
        return merged

    latest_evidence = StripeCapabilityEvidence.from_payload(latest)
    if latest_evidence.amount_minor is None:
        return merged

    total_summary = dict(merged.get("total_summary") or {})
    total_summary["due"] = latest_evidence.amount_minor
    if latest_evidence.currency:
        total_summary["currency"] = latest_evidence.currency
    merged["total_summary"] = total_summary
    return merged


def _promotion_error_text(exc: Exception) -> str:
    """Return a stable, credential-free promotion failure label."""
    if isinstance(exc, WalletProviderError):
        return str(exc.error_code or "promotion_failed")
    status_code = getattr(exc, "status_code", 0)
    if status_code:
        return f"{exc.__class__.__name__}_http_{status_code}"
    return exc.__class__.__name__


def _promotion_response_diagnostics(
    response: Any,
    *,
    contract: CheckoutRequestContract,
    session: CheckoutSessionContract,
    transport_context: Mapping[str, Any],
    attempt: int = 0,
    request_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostics = _promotion_stage_diagnostics(session, transport_context)
    if attempt:
        diagnostics["attempt"] = attempt
    diagnostics["request_checkout_session_id_matches"] = (
        str((request_payload or {}).get("checkout_session_id") or "").strip()
        == str(session.checkout_session_id or "").strip()
    )
    diagnostics["request_promo_campaign_id"] = str(
        ((request_payload or {}).get("promo_campaign") or {}).get("promo_campaign_id")
        if isinstance((request_payload or {}).get("promo_campaign"), Mapping)
        else ""
    )
    diagnostics["response_type"] = type(response).__name__
    if not isinstance(response, Mapping):
        diagnostics["response_object"] = False
        return diagnostics
    diagnostics["response_object"] = True
    diagnostics["response_keys"] = sorted(str(key) for key in response.keys())[:16]
    if "success" in response:
        diagnostics["success"] = bool(response.get("success"))
    evidence = StripeCapabilityEvidence.from_payload(
        response,
        fallback_currency=contract.currency,
    )
    diagnostics["amount_minor"] = evidence.amount_minor
    diagnostics["currency"] = evidence.currency
    diagnostics["has_zero_amount_evidence"] = evidence.amount_minor == 0
    diagnostics["has_amount_evidence"] = evidence.amount_minor is not None
    diagnostics.update(_promotion_payload_shape_diagnostics(response))
    error_hint = _promotion_response_error_hint(response)
    if error_hint:
        diagnostics["error_hint"] = error_hint
    return diagnostics


def _promotion_exception_diagnostics(
    exc: Exception,
    *,
    session: CheckoutSessionContract,
    transport_context: Mapping[str, Any],
    attempt: int = 0,
) -> dict[str, Any]:
    diagnostics = _promotion_stage_diagnostics(session, transport_context)
    if attempt:
        diagnostics["attempt"] = attempt
    diagnostics["exception_type"] = type(exc).__name__
    diagnostics["error_code"] = (
        getattr(exc, "error_code", "")
        or getattr(exc, "code", "")
        or _promotion_error_text(exc)
    )
    status_code = _exception_status_code(exc)
    if status_code:
        diagnostics["http_status"] = status_code
    diagnostics["retryable"] = bool(getattr(exc, "retryable", False))
    diagnostics["terminal_status"] = str(getattr(exc, "status", "") or "")
    return diagnostics


def _promotion_payload_shape_diagnostics(response: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize checkout/update payload shape without logging credentials."""
    nested_paths = _collect_nested_key_paths(
        response,
        target_keys={
            "amount",
            "amount_due",
            "amountDue",
            "amount_total",
            "amountTotal",
            "checkout_amount_minor",
            "currency",
            "currency_code",
            "currencyCode",
            "discount",
            "discounts",
            "due",
            "promo_campaign",
            "promotion",
            "subtotal",
            "total",
            "total_summary",
            "totalSummary",
        },
        max_paths=40,
    )
    amount_candidates = _collect_amount_candidates(response, max_candidates=24)
    zero_paths = [
        str(item.get("path") or "")
        for item in amount_candidates
        if item.get("amount_minor") == 0
    ][:12]
    checkout_session = response.get("checkout_session")
    checkout_keys: list[str] = []
    if isinstance(checkout_session, Mapping):
        checkout_keys = sorted(str(key) for key in checkout_session.keys())[:24]
    elif isinstance(checkout_session, str):
        checkout_keys = ["<string>"]
    return {
        "checkout_session_value_type": type(checkout_session).__name__ if "checkout_session" in response else "",
        "checkout_session_keys": checkout_keys,
        "nested_key_paths": nested_paths,
        "amount_candidates": amount_candidates,
        "zero_amount_paths": zero_paths,
        "amount_candidate_count": len(amount_candidates),
    }


def _collect_nested_key_paths(
    value: Any,
    *,
    target_keys: set[str],
    max_paths: int,
    path: tuple[str, ...] = (),
    depth: int = 0,
    seen: set[int] | None = None,
) -> list[str]:
    if max_paths <= 0 or depth > 10:
        return []
    if seen is None:
        seen = set()
    output: list[str] = []
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return []
        seen.add(identity)
        for key, item in value.items():
            key_text = str(key)
            child_path = (*path, key_text)
            if key_text in target_keys:
                output.append(".".join(child_path))
                if len(output) >= max_paths:
                    return output
            if isinstance(item, (Mapping, list, tuple)):
                output.extend(_collect_nested_key_paths(
                    item,
                    target_keys=target_keys,
                    max_paths=max_paths - len(output),
                    path=child_path,
                    depth=depth + 1,
                    seen=seen,
                ))
                if len(output) >= max_paths:
                    return output
    elif isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            return []
        seen.add(identity)
        for index, item in enumerate(value[:20] if isinstance(value, list) else list(value)[:20]):
            if isinstance(item, (Mapping, list, tuple)):
                output.extend(_collect_nested_key_paths(
                    item,
                    target_keys=target_keys,
                    max_paths=max_paths - len(output),
                    path=(*path, f"[{index}]"),
                    depth=depth + 1,
                    seen=seen,
                ))
                if len(output) >= max_paths:
                    return output
    return output


def _collect_amount_candidates(
    value: Any,
    *,
    max_candidates: int,
    path: tuple[str, ...] = (),
    depth: int = 0,
    seen: set[int] | None = None,
) -> list[dict[str, Any]]:
    if max_candidates <= 0 or depth > 10:
        return []
    if seen is None:
        seen = set()
    output: list[dict[str, Any]] = []
    amount_keys = {
        "amount",
        "amount_due",
        "amountDue",
        "amount_total",
        "amountTotal",
        "checkout_amount_minor",
        "due",
        "minorUnitsAmount",
        "minor_units_amount",
        "subtotal",
        "taxInclusive",
        "taxInclusiveAmount",
        "total",
        "unit_amount",
        "value",
    }
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return []
        seen.add(identity)
        for key, item in value.items():
            key_text = str(key)
            child_path = (*path, key_text)
            if key_text in amount_keys:
                parsed = _diagnostic_minor_units(item)
                if parsed is not None:
                    output.append({
                        "path": ".".join(child_path),
                        "amount_minor": parsed,
                    })
                    if len(output) >= max_candidates:
                        return output
            if isinstance(item, (Mapping, list, tuple)):
                output.extend(_collect_amount_candidates(
                    item,
                    max_candidates=max_candidates - len(output),
                    path=child_path,
                    depth=depth + 1,
                    seen=seen,
                ))
                if len(output) >= max_candidates:
                    return output
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in seen:
            return []
        seen.add(identity)
        for index, item in enumerate(list(value)[:20]):
            if isinstance(item, (Mapping, list, tuple)):
                output.extend(_collect_amount_candidates(
                    item,
                    max_candidates=max_candidates - len(output),
                    path=(*path, f"[{index}]"),
                    depth=depth + 1,
                    seen=seen,
                ))
                if len(output) >= max_candidates:
                    return output
    return output


def _diagnostic_minor_units(value: Any) -> int | None:
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
    if isinstance(value, Mapping):
        for key in ("minorUnitsAmount", "minor_units_amount", "amount", "value", "unit_amount", "amount_minor"):
            if key in value:
                parsed = _diagnostic_minor_units(value[key])
                if parsed is not None:
                    return parsed
    return None


def _promotion_stage_diagnostics(
    session: CheckoutSessionContract,
    transport_context: Mapping[str, Any],
) -> dict[str, Any]:
    countries = transport_context.get("stage_proxy_countries")
    promotion_country = ""
    checkout_country = ""
    if isinstance(countries, Mapping):
        promotion_country = str(countries.get("promotion") or "").strip().upper()
        checkout_country = str(countries.get("checkout") or "").strip().upper()
    return {
        "checkout_session_id_hint": _identifier_hint(session.checkout_session_id),
        "processor_entity": str(session.processor_entity or ""),
        "promotion_country": promotion_country,
        "checkout_country": checkout_country,
        "promotion_proxy_configured": bool(str(transport_context.get("promotion_proxy") or "").strip()),
        "checkout_proxy_configured": bool(str(transport_context.get("checkout_proxy") or "").strip()),
        "same_checkout_and_promotion_proxy": _same_configured_proxy(
            transport_context.get("checkout_proxy"),
            transport_context.get("promotion_proxy"),
        ),
    }


def _promotion_response_error_hint(response: Mapping[str, Any]) -> str:
    candidates: list[Any] = []
    for key in ("error", "error_code", "message", "detail"):
        value = response.get(key)
        if value not in (None, ""):
            candidates.append(value)
    nested = response.get("data")
    if isinstance(nested, Mapping):
        for key in ("error", "error_code", "message", "detail"):
            value = nested.get(key)
            if value not in (None, ""):
                candidates.append(value)
    if not candidates:
        return ""
    return redact_sensitive_text(str(candidates[0]))[:160]


def _same_configured_proxy(left: Any, right: Any) -> bool | None:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return None
    return left_text == right_text


def _gopay_create_with_promo(transport_context: Mapping[str, Any]) -> bool:
    """Whether GoPay should include promo_campaign in the initial checkout create."""
    if "gopay_create_with_promo" not in transport_context:
        return True
    value = transport_context.get("gopay_create_with_promo")
    if value is None or value == "":
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}




def _gopay_skip_promotion_update(
    transport_context: Mapping[str, Any],
    create_had_promo: bool,
) -> bool:
    """Whether GoPay should skip the checkout/update re-apply.

    Live evidence shows the initial checkout create with promo_campaign
    already attaches the plus-1-month-free 100% coupon; a follow-up
    checkout/update can leave Stripe serving the full-price snapshot.
    Plain (promo-less) creates still need the update to attach the campaign.
    """
    if not create_had_promo:
        return False
    if "gopay_promotion_update" in transport_context:
        value = transport_context.get("gopay_promotion_update")
        if value is None or value == "":
            return True
        if isinstance(value, bool):
            return not value
        return str(value).strip().lower() in {"0", "false", "no", "off"}
    return True

def _promotion_attempt_limit(transport_context: Mapping[str, Any]) -> int:
    """Bound promotion retries so a stale/nonzero session cannot loop forever."""
    raw = transport_context.get("promotion_attempts", _DEFAULT_PROMOTION_ATTEMPTS)
    try:
        return max(1, min(int(raw or _DEFAULT_PROMOTION_ATTEMPTS), 5))
    except (TypeError, ValueError):
        return _DEFAULT_PROMOTION_ATTEMPTS


def _structured_error(exc: Exception, stage: str) -> WalletProviderError:
    if isinstance(exc, WalletProviderError):
        return exc
    if isinstance(exc, CheckoutContractError):
        return WalletProviderError(
            str(exc),
            error_code=getattr(exc, "error_code", "checkout_contract_invalid"),
            error_stage=getattr(exc, "error_stage", stage),
            retryable=bool(getattr(exc, "retryable", False)),
        )
    if isinstance(exc, TimeoutError) and stage in _SIDE_EFFECT_STAGES:
        return WalletUnknownResultError(
            str(exc) or "wallet transport timed out after a side effect",
            error_stage=stage,
        )
    if isinstance(exc, TimeoutError):
        return WalletTimedOutError(str(exc) or "wallet transport timed out", error_stage=stage)
    status_code = _exception_status_code(exc)
    retryable = status_code == 429 or status_code >= 500 if status_code else not isinstance(exc, (TypeError, ValueError))
    uncertain = stage in _SIDE_EFFECT_STAGES and retryable
    return WalletProviderError(
        str(exc) or exc.__class__.__name__,
        error_code="wallet_transport_error",
        error_stage=stage,
        retryable=False if uncertain else retryable,
        status="unknown" if uncertain else "failed",
    )


def _exception_status_code(exc: Exception) -> int:
    for value in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None)):
        try:
            status = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status
    return 0


def _failure_result(
    spec: WalletMethodSpec | None,
    error: WalletProviderError,
    *,
    capability: Mapping[str, Any] | None = None,
    checkout_session_type: str = "",
    capability_source: str = "",
    promotion_applied: bool | None = None,
    promotion_error: str = "",
    promotion_attempt_count: int = 0,
    promotion_diagnostics: Mapping[str, Any] | None = None,
    checkout_ui_mode: str = "",
    initial_checkout_has_promo: bool | None = None,
    checkout_page_checked: bool = False,
    checkout_page_amount: int | None = None,
    checkout_page_error: str = "",
    checkout_session_id: str = "",
    checkout_url: str = "",
    checkout_eligibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "ok": False,
        "status": error.status,
        "payment_method": spec.key if spec else "",
        "url": "",
        "error": str(error),
        "error_code": error.error_code,
        "retryable": error.retryable,
        "error_stage": error.error_stage,
    }
    if checkout_eligibility:
        result["checkout_eligibility"] = dict(checkout_eligibility)
    if capability is not None:
        result["capability"] = dict(capability)
    if checkout_session_type:
        result["checkout_session_type"] = checkout_session_type
    if capability_source:
        result["capability_source"] = capability_source
    if promotion_applied is not None:
        result["promotion_applied"] = promotion_applied
    if promotion_error:
        result["promotion_error"] = promotion_error
    if promotion_attempt_count:
        result["promotion_attempt_count"] = promotion_attempt_count
    if promotion_diagnostics:
        result["promotion_diagnostics"] = dict(promotion_diagnostics)
    if checkout_ui_mode:
        result["checkout_ui_mode"] = checkout_ui_mode
    if initial_checkout_has_promo is not None:
        result["initial_checkout_has_promo"] = initial_checkout_has_promo
    if checkout_page_checked:
        result["checkout_page_checked"] = True
    if checkout_page_amount is not None:
        result["checkout_page_amount_minor"] = checkout_page_amount
    if checkout_page_error:
        result["checkout_page_error"] = checkout_page_error
    if checkout_session_id:
        result["checkout_session_id_hint"] = _identifier_hint(checkout_session_id)
    if checkout_url:
        result["checkout_url"] = checkout_url
    if error.status == "unknown":
        result["retryable"] = False
        result["requires_reconciliation"] = True
    return result


def _resume_checkout_session(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a persisted checkout session for reuse across runs."""
    if not isinstance(value, Mapping):
        return {}
    session_id = str(
        value.get("checkout_session_id")
        or value.get("session_id")
        or value.get("id")
        or ""
    ).strip()
    if not session_id.startswith(("cs_", "oaics_")):
        return {}
    return {
        "checkout_session_id": session_id,
        "processor_entity": str(value.get("processor_entity") or "").strip(),
        "publishable_key": str(value.get("publishable_key") or "").strip(),
    }


def _checkout_eligibility_diagnostics(
    payload: Any,
    requested: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract promo-eligibility signals from a checkout create response.

    OpenAI decides trial eligibility server-side; the response exposes it via
    ``one_click_trial_eligible`` and discount-preview fields. Surfacing these
    lets callers distinguish "promo not applied" from "account has no trial".
    """
    if not isinstance(payload, Mapping):
        return {}
    diag: dict[str, Any] = {}
    trial = payload.get("one_click_trial_eligible")
    if isinstance(trial, bool):
        diag["one_click_trial_eligible"] = trial
    diag["promo_campaign_requested"] = bool(requested.get("promo_campaign"))
    diag["promo_campaign_echoed"] = isinstance(payload.get("promo_campaign"), Mapping)
    for key in (
        "scheduled_discount_preview",
        "immediate_discount_settings",
        "promo_credit_grant",
        "credit_discount_offer",
        "is_new_stripe_customer",
        "requires_manual_approval",
    ):
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None or isinstance(value, (bool, int, str)):
            diag[key] = value
        elif isinstance(value, Mapping):
            diag[key] = sorted(str(k) for k in value.keys())[:8]
        else:
            diag[key] = type(value).__name__
    return diag


def _not_zero_due_message(
    amount: int | None,
    currency: str,
    eligibility: Mapping[str, Any],
) -> str:
    """Zero-due failure message with the server-side trial signal attached."""
    message = f"wallet checkout is not zero due: amount={amount} currency={currency}"
    if eligibility.get("one_click_trial_eligible") is False:
        message += "; account is not trial-eligible (one_click_trial_eligible=false)"
    return message

def _identifier_hint(value: str) -> str:
    text = str(value or "")
    if len(text) <= 12:
        return "[REDACTED_ID]" if text else ""
    return f"{text[:7]}...{text[-4:]}"


__all__ = [
    "WALLET_METHODS",
    "WalletCancelledError",
    "WalletFlowIdentifiers",
    "WalletMethodSpec",
    "WalletProviderError",
    "WalletProviderTransport",
    "WalletTimedOutError",
    "WalletTransportRequest",
    "WalletUnknownResultError",
    "build_confirm_payload",
    "build_payment_page_poll_payload",
    "build_payment_method_payload",
    "capability_result",
    "random_billing_details",
    "redact_sensitive_text",
    "run_wallet_provider",
    "wallet_method_spec",
]
