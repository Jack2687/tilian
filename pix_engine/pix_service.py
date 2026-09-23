"""PIX Custom Checkout extraction engine.

The implementation follows the Brazilian PIX request order captured in
``0元风控.har``.  It deliberately treats the HAR's ``generic_decline`` as a
terminal upstream result; a declined checkout is never converted into a link.

The transport is injectable so the parser and state machine can be verified
without touching the network.  Production calls use curl-cffi with one stable
browser/session identity for the complete attempt.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - imported only on a live run
    curl_requests = None


PIX_COUNTRY = "BR"
PIX_CURRENCY = "brl"
# 国家币种自由选择：默认巴西（BR/BRL），用户可在前端选择其他建单国家与币种。
# 与 GoPay 引擎的 _COUNTRY_CURRENCY 保持一致，币种未显式指定时按国家推断。
_COUNTRY_CURRENCY = {
    "BR": "BRL", "ID": "IDR", "PH": "PHP", "US": "USD", "GB": "GBP", "DE": "EUR",
    "JP": "JPY", "VN": "VND", "TH": "THB", "IN": "INR", "TR": "TRY", "MX": "MXN",
    "AR": "ARS", "CL": "CLP", "CO": "COP", "PE": "PEN", "SA": "SAR", "AE": "AED",
    "NG": "NGN", "ZA": "ZAR", "KR": "KRW", "MY": "MYR", "SG": "SGD", "AU": "AUD",
    "NZ": "NZD", "CA": "CAD", "HK": "HKD", "TW": "TWD",
}
# Stripe/浏览器 locale 随建单国家切换（BR 默认 pt-BR，其余按国家近似映射）。
_COUNTRY_LOCALE = {
    "BR": "pt-BR", "ID": "id-ID", "PH": "en-PH", "US": "en-US", "GB": "en-GB",
    "DE": "de-DE", "JP": "ja-JP", "VN": "vi-VN", "TH": "th-TH", "IN": "en-IN",
    "TR": "tr-TR", "MX": "es-MX", "AR": "es-AR", "CL": "es-CL", "CO": "es-CO",
    "PE": "es-PE", "SA": "ar-SA", "AE": "en-AE", "NG": "en-NG", "ZA": "en-ZA",
    "KR": "ko-KR", "MY": "ms-MY", "SG": "en-SG", "AU": "en-AU", "NZ": "en-NZ",
    "CA": "en-CA", "HK": "zh-HK", "TW": "zh-TW",
}
PIX_PROMO = "plus-1-month-free"
STRIPE_VERSION = "2025-03-31.basil"
STRIPE_CUSTOM_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
STRIPE_RUNTIME_VERSION = os.environ.get("PIX_STRIPE_RUNTIME_VERSION", "b0f5e7abe5")
# OpenAI 网页客户端版本头（bax.har 2026-09-08 观测值）。版本陈旧本身就是
# 机器人特征（会直接放大 requires_manual_approval / blocked 风控），因此默认值
# 随最新抓包滚动；部署侧可用 PIX_CHATGPT_CLIENT_VERSION / PIX_CHATGPT_CLIENT_BUILD
# 覆盖，warmup 抓到的页面若含 prod-<40hex> 版本串也会自动升级。
CHATGPT_CLIENT_VERSION = os.environ.get("PIX_CHATGPT_CLIENT_VERSION") or "prod-8b282ae6b2e95fa180d7bde7b664e1446691fac0"
CHATGPT_CLIENT_BUILD = os.environ.get("PIX_CHATGPT_CLIENT_BUILD") or "10379027"
DEFAULT_PROXY = ""
MAX_POLL_ATTEMPTS = 120

# ── OpenAI custom checkout "server updates" 链路（2026-09 bax.har 对齐）──
# 新版 chatgpt.com/checkout/{entity}/{id} 页面不再走 Stripe payment_pages
# init/confirm（legacy），而是：elements/sessions（customer session secret）
# → /v1/confirmation_tokens（ctoken）→ /backend-api/payments/checkout/confirm
# （JSON：checkout_session_id + confirm_token + selected_payment_method_type）。
STRIPE_BASIL_VERSION = "2025-03-31.basil"
# 页面 stripe.js 的 deploy revision 前 10 位（bax.har 观测值；随部署滚动，
# 仅作 attribution 用，可经 PIX_STRIPE_JS_REVISION 注入更新值）。
STRIPE_JS_REVISION = os.environ.get("PIX_STRIPE_JS_REVISION", "8444022167")
# auto=优先新链路，契约错误时回退 legacy；token=强制新链路；legacy=强制旧链路。
# 注入 transport 的测试环境默认 legacy（fixture 是 legacy 端点集）。
_OA_CONFIRM_DEFAULT = os.environ.get("PIX_OA_CONFIRM_PROTOCOL", "auto").strip().lower()

# Realistic Brazilian identity pools.  Each run draws a fresh random profile so
# repeated attempts do not reuse the same CPF/CNPJ or fixed address.
_BR_FIRST_NAMES = (
    "Lucas", "Gabriel", "Rafael", "Matheus", "Pedro", "Joao", "Gustavo",
    "Felipe", "Thiago", "Bruno", "Caio", "Diego", "Enzo", "Davi", "Samuel",
    "Vinicius", "Igor", "Murilo", "Andre", "Renan", "Mariana", "Beatriz",
    "Camila", "Larissa", "Juliana", "Fernanda", "Patricia", "Vanessa",
    "Amanda", "Bruna", "Carolina", "Debora", "Gabriela", "Helena", "Isabela",
    "Julia", "Leticia", "Natalia", "Raquel", "Sofia",
)
_BR_LAST_NAMES = (
    "Silva", "Santos", "Oliveira", "Souza", "Pereira", "Costa", "Rodrigues",
    "Almeida", "Nascimento", "Lima", "Araujo", "Carvalho", "Gomes", "Martins",
    "Rocha", "Ribeiro", "Alves", "Monteiro", "Cardoso", "Moreira", "Barbosa",
    "Ferreira", "Mendes", "Pinto", "Correia", "Teixeira", "Melo", "Freitas",
    "Fernandes", "Vieira", "Campos", "Pires",
)
# (street, city, state, postal) — real Brazilian addresses with matching CEP.
_BR_ADDRESSES = (
    ("Avenida Paulista 1000", "Sao Paulo", "SP", "01310-100"),
    ("Avenida Paulista 1578", "Sao Paulo", "SP", "01310-200"),
    ("Rua Augusta 2500", "Sao Paulo", "SP", "01412-000"),
    ("Rua Oscar Freire 900", "Sao Paulo", "SP", "01426-001"),
    ("Avenida Brigadeiro Faria Lima 1500", "Sao Paulo", "SP", "01452-001"),
    ("Rua da Assembleia 10", "Rio de Janeiro", "RJ", "20011-901"),
    ("Avenida Atlantica 2000", "Rio de Janeiro", "RJ", "22021-001"),
    ("Rua Visconde de Piraja 500", "Rio de Janeiro", "RJ", "22410-003"),
    ("Avenida Afonso Pena 1500", "Belo Horizonte", "MG", "30130-005"),
    ("Rua da Bahia 1000", "Belo Horizonte", "MG", "30160-011"),
    ("Avenida Sete de Setembro 800", "Salvador", "BA", "40060-001"),
    ("Rua Chile 15", "Salvador", "BA", "40020-000"),
    ("Rua XV de Novembro 500", "Curitiba", "PR", "80020-310"),
    ("Avenida Batel 1200", "Curitiba", "PR", "80420-090"),
    ("Rua dos Andradas 1000", "Porto Alegre", "RS", "90020-004"),
    ("Avenida Ipiranga 6681", "Porto Alegre", "RS", "90619-900"),
    ("Avenida Boa Viagem 500", "Recife", "PE", "51011-000"),
    ("Rua do Bom Jesus 100", "Recife", "PE", "50030-240"),
    ("Avenida Beira Mar 1000", "Fortaleza", "CE", "60165-000"),
    ("Rua Barao do Rio Branco 100", "Fortaleza", "CE", "60025-020"),
    ("Avenida das Nacoes 100", "Brasilia", "DF", "70330-000"),
    ("Rua 10 Norte 100", "Brasilia", "DF", "70002-900"),
)


def generate_valid_cpf() -> str:
    """Generate a CPF with correct mod-11 check digits (11 digits)."""
    while True:
        digits = [random.randint(0, 9) for _ in range(9)]
        if len(set(digits)) > 1:
            break
    for _ in range(2):
        total = sum((len(digits) + 1 - i) * v for i, v in enumerate(digits))
        digits.append((total * 10) % 11 if (total * 10) % 11 < 10 else 0)
    return "".join(str(d) for d in digits)


def generate_valid_cnpj() -> str:
    """Generate a CNPJ with correct mod-11 check digits (14 digits)."""
    while True:
        digits = [random.randint(0, 9) for _ in range(12)]
        if len(set(digits)) > 1:
            break
    for weights in ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)):
        total = sum(number * weight for number, weight in zip(digits, weights))
        digit = 11 - (total % 11)
        digits.append(0 if digit >= 10 else digit)
    return "".join(str(d) for d in digits)


def _cpf_check_digits(base: str) -> tuple[str, str]:
    digits = [int(d) for d in str(base or "")]
    for weights in (range(10, 1, -1), range(11, 1, -1)):
        total = sum(number * weight for number, weight in zip(digits, weights))
        value = 11 - (total % 11)
        digits.append(0 if value >= 10 else value)
    return tuple(str(d) for d in digits[-2:])


def _cnpj_check_digits(base: str) -> tuple[str, str]:
    digits = [int(d) for d in str(base or "")]
    for weights in ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)):
        total = sum(number * weight for number, weight in zip(digits, weights))
        value = 11 - (total % 11)
        digits.append(0 if value >= 10 else value)
    return tuple(str(d) for d in digits[-2:])


def is_valid_cpf(value: str) -> bool:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    return "".join(_cpf_check_digits(digits[:9])) == digits[9:]


def is_valid_cnpj(value: str) -> bool:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 14 or len(set(digits)) == 1:
        return False
    return "".join(_cnpj_check_digits(digits[:12])) == digits[12:]


def _normalize_tax_kind(kind: str) -> str:
    value = str(kind or os.environ.get("PIX_TAX_KIND") or "random").strip().lower()
    if value in {"cpf", "cnpj"}:
        return value
    # Only registered CNPJs are verifiable through a public API.  A random
    # checksum-valid CPF cannot be confirmed as real, so the default resolves
    # to the real registered CNPJ pool to keep the PIX PSP happy.
    return "cnpj"


# Real registered CNPJs (Receita Federal ATIVA) with their official registered
# address.  Stripe's PIX partner PSP validates the tax id against the Brazilian
# registry at payment time: a checksum-valid but unregistered number is refused
# with partner_generic_decline (observed live).  Only registered CNPJs pass,
# so the CNPJ branch of random_brazil_billing draws from this pool instead of
# fabricating a number.  (Verified 2026-08 via BrasilAPI /api/cnpj/v1.)
_BR_REAL_CNPJ_POOL = (
    {
        "tax_id": "11222333000181",
        "name": "CAIXA ESCOLAR DA ESCOLA ESTADUAL DE ENSINO FUNDAMENTAL JOSEFINA JACQUES NORONHA",
        "line1": "GARIBALDI 070",
        "city": "SAO SEBASTIAO DO CAI",
        "state": "RS",
        "postal_code": "95760-000",
    },
    {
        "tax_id": "33000167000101",
        "name": "PETROLEO BRASILEIRO S A PETROBRAS",
        "line1": "REPUBLICA DO CHILE 65",
        "city": "RIO DE JANEIRO",
        "state": "RJ",
        "postal_code": "20031-170",
    },
    {
        "tax_id": "00000000000191",
        "name": "BANCO DO BRASIL SA",
        "line1": "SAUN QUADRA 5 BLOCO B TORRE I, II, III",
        "city": "BRASILIA",
        "state": "DF",
        "postal_code": "70040-912",
    },
    {
        "tax_id": "33592510000154",
        "name": "VALE S.A.",
        "line1": "BOTAFOGO 00186",
        "city": "RIO DE JANEIRO",
        "state": "RJ",
        "postal_code": "22250-145",
    },
    {
        "tax_id": "07526557000100",
        "name": "AMBEV S.A.",
        "line1": "DR RENATO PAES DE BARROS 1017",
        "city": "SAO PAULO",
        "state": "SP",
        "postal_code": "04530-001",
    },
    {
        "tax_id": "47960950000121",
        "name": "MAGAZINE LUIZA S/A",
        "line1": "VOLUNTARIOS DA FRANCA 1465",
        "city": "FRANCA",
        "state": "SP",
        "postal_code": "14400-490",
    },
    {
        "tax_id": "71673990000177",
        "name": "NATURA COSMETICOS S/A",
        "line1": "ALEXANDRE COLARES 1188",
        "city": "SAO PAULO",
        "state": "SP",
        "postal_code": "05106-000",
    },
    {
        "tax_id": "60701190000104",
        "name": "ITAU UNIBANCO S.A.",
        "line1": "ALFREDO EGYDIO DE SOUZA ARANHA 100",
        "city": "SAO PAULO",
        "state": "SP",
        "postal_code": "04344-902",
    },
    {
        "tax_id": "60746948000112",
        "name": "BANCO BRADESCO S.A.",
        "line1": "CIDADE DE DEUS S/N",
        "city": "OSASCO",
        "state": "SP",
        "postal_code": "06029-900",
    },
    {
        "tax_id": "00360305000104",
        "name": "CAIXA ECONOMICA FEDERAL",
        "line1": "SETOR SBS S/N",
        "city": "BRASILIA",
        "state": "DF",
        "postal_code": "70092-900",
    },
    {
        "tax_id": "33611500000119",
        "name": "GERDAU S.A.",
        "line1": "DOUTORA RUTH CARDOSO 8501",
        "city": "SAO PAULO",
        "state": "SP",
        "postal_code": "05425-070",
    },
    {
        "tax_id": "07689002000189",
        "name": "EMBRAER S.A.",
        "line1": "BRIGADEIRO FARIA LIMA 2170",
        "city": "SAO JOSE DOS CAMPOS",
        "state": "SP",
        "postal_code": "12227-901",
    },
)


def random_brazil_billing(email: str = "", tax_kind: str = "") -> dict[str, str]:
    """Build a fresh random Brazilian billing profile.

    kind=cpf: random checksum-valid CPF plus a realistic person name/address.
    kind=cnpj: a REAL registered CNPJ drawn from _BR_REAL_CNPJ_POOL with its
    official registered name and address — required because Stripe's PIX PSP
    validates the tax id against Receita Federal and refuses unregistered
    numbers with partner_generic_decline (observed live).
    Explicit caller values are never touched; this only fills fields the
    caller left empty, so every PIX attempt gets a valid tax id and a
    realistic BR address instead of the fixed default profile.
    """
    kind = _normalize_tax_kind(tax_kind)
    first = random.choice(_BR_FIRST_NAMES)
    last = random.choice(_BR_LAST_NAMES)
    if kind == "cnpj":
        company = random.choice(_BR_REAL_CNPJ_POOL)
        name = str(company["name"])
        line1 = str(company["line1"])
        city = str(company["city"])
        state = str(company["state"])
        postal_code = str(company["postal_code"])
        tax_id = str(company["tax_id"])
    else:
        line1, city, state, postal_code = random.choice(_BR_ADDRESSES)
        tax_id = generate_valid_cpf()
        name = f"{first} {last}"
    fallback_email = email or f"{first.lower()}.{last.lower()}{random.randint(100, 999)}@outlook.com"
    return {
        "name": name,
        "email": fallback_email,
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "country": PIX_COUNTRY,
        "tax_id": tax_id,
    }


Transport = Callable[..., tuple[int, str, dict[str, str]]]


def _json_response(status: int, text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} returned invalid JSON (HTTP {status})") from exc
    if status >= 400:
        detail = value.get("error") if isinstance(value, dict) else value
        if isinstance(detail, dict):
            # Stripe errors carry the actionable field in `param`; keep it in
            # the exception so a malformed request is distinguishable from a
            # risk/issuer decline at a later stage.
            code = str(detail.get("code") or "").strip()
            param = str(detail.get("param") or "").strip()
            message = str(detail.get("message") or "").strip()
            parts = [item for item in (code, f"param={param}" if param else "", message) if item]
            detail = "; ".join(parts) or detail
        # 保留完整上游响应（如 OpenAI FastAPI 的 {"detail": ...}），便于定位
        # confirm/建单阶段的 400 真因；截断仅用于网络错误场景防刷屏。
        raise RuntimeError(f"{label} HTTP {status}: {str(detail or text)[:1500]}")
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned a non-object JSON payload")
    return value


_ATTESTATION_RE = re.compile(r'"webDeploymentAttestation"\s*:\s*"([^"]+)"')
_ATTESTATION_ALT_RE = re.compile(r'[^A-Za-z0-9]webDeploymentAttestation[=:]\s*["\']?([A-Za-z0-9._-]+)')


def _stripe_error_details(text: str) -> tuple[str, str, str]:
    """Return Stripe's structured error code, parameter and message."""
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return "", "", ""
    detail = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        return "", "", ""
    return (
        str(detail.get("code") or "").strip().lower(),
        str(detail.get("param") or "").strip(),
        str(detail.get("message") or "").strip(),
    )


def _drop_stripe_param(form: dict[str, str], param: str) -> bool:
    """Drop only the named form field (or its nested children)."""
    name = str(param or "").strip()
    if not name:
        return False
    removed = False
    for key in list(form):
        if key == name or key.startswith(name + "["):
            form.pop(key, None)
            removed = True
    return removed


def _is_stripe_parameter_error(code: str) -> bool:
    return str(code or "").lower() in {
        "parameter_invalid_empty",
        "parameter_unknown",
        "parameter_missing",
    }


def _first(data: Any, keys: tuple[str, ...]) -> str:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
        for value in data.values():
            found = _first(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _first(value, keys)
            if found:
                return found
    return ""


def _token_from(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("bearer "):
            text = text[7:].strip()
        return text
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token"):
            found = _token_from(value.get(key))
            if found:
                return found
        for item in value.values():
            found = _token_from(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _token_from(item)
            if found:
                return found
    return ""


def normalize_access_token(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("{") or text.startswith("["):
        try:
            return _token_from(json.loads(text))
        except json.JSONDecodeError:
            return ""
    return _token_from(text)


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        value = json.loads(base64.urlsafe_b64decode(part))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _jwt_account_id(token: str) -> str:
    auth = _jwt_payload(token).get("https://api.openai.com/auth")
    return str(auth.get("chatgpt_account_id") or "").strip() if isinstance(auth, dict) else ""


def _jwt_email(token: str) -> str:
    payload = _jwt_payload(token)
    profile = payload.get("https://api.openai.com/profile")
    candidates = [profile.get("email"), profile.get("email_address")] if isinstance(profile, dict) else []
    candidates.extend([payload.get("email"), payload.get("preferred_username")])
    for value in candidates:
        if "@" in str(value or ""):
            return str(value).strip()
    return "buyer@example.com"


def _checkout_country_currency(country: str, currency: str) -> tuple[str, str]:
    """Resolve the effective checkout country/currency, defaulting to BR/BRL."""
    cc = str(country or "").strip().upper() or PIX_COUNTRY
    cur = str(currency or "").strip().upper() or _COUNTRY_CURRENCY.get(cc, PIX_CURRENCY.upper())
    return cc, cur


def _checkout_locale(country: str) -> str:
    return _COUNTRY_LOCALE.get(str(country or "").strip().upper(), "pt-BR")


def _name_from_email(email: str) -> str:
    """Derive a display name from an email local-part without hardcoding a country."""
    text = str(email or "").strip()
    local = text.split("@", 1)[0] if "@" in text else text
    parts = [part for part in re.split(r"[._\-+\s]+", local) if part]
    if not parts:
        return "Buyer"
    cleaned = [part.capitalize() for part in parts[:2]]
    return " ".join(cleaned)


def normalize_proxy(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" in text:
        return text
    parts = text.split(":", 3)
    if len(parts) == 4 and parts[1].isdigit():
        host, port, user, password = parts
        return f"http://{quote(user, safe='-._~')}:{quote(password, safe='-._~')}@{host}:{port}"
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{parts[0]}:{parts[1]}"
    return text


def _amount(payload: Any) -> int | None:
    """Find the payable total, preferring total/due over subtotal/recurring values."""
    if isinstance(payload, dict):
        for key in ("amount_total", "amount_due", "total", "due", "minorUnitsAmount", "minor_units_amount"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
            if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
                return int(value)
        for key in ("total_summary", "checkout_state", "checkout_session", "invoice", "tax", "payment_intent"):
            if key in payload:
                found = _amount(payload[key])
                if found is not None:
                    return found
        for value in payload.values():
            found = _amount(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _amount(value)
            if found is not None:
                return found
    return None


def _int_amount(value: Any) -> int | None:
    """Coerce a Stripe minor-units amount to int, or None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def _checkout_total_minor(payload: Any) -> int | None:
    """Read the authoritative final total from a checkout create / taxes payload.

    The custom checkout JSON nests the discounted total under
    checkout_state.total.total.minorUnitsAmount (bax.har: subtotal 9990,
    discount 9990, total 0).  The generic _amount() walker would grab the
    subtotal's minorUnitsAmount first, so this helper reads the nested total
    node explicitly for the confirm-token flow.
    """
    if isinstance(payload, dict):
        for key in ("checkout_state", "checkout_session"):
            node = payload.get(key)
            if not isinstance(node, dict):
                continue
            total = node.get("total")
            if not isinstance(total, dict):
                continue
            inner = total.get("total")
            if isinstance(inner, dict):
                minor = _int_amount(inner.get("minorUnitsAmount"))
                if minor is not None:
                    return minor
                minor = _int_amount(inner.get("minor_units_amount"))
                if minor is not None:
                    return minor
        # Fallback: prefer explicit total keys over subtotal across the payload.
        found = _payable_amount(payload)
        if found is not None:
            return found
    return None


def _elements_session_parts(payload: Any) -> tuple[str, str, str]:
    """Extract (elements_session_id, config_id, stripe_customer_id) from an
    /v1/elements/sessions response (confirm-token flow).  All three are best
    effort: missing values are omitted from the later confirmation_tokens form
    instead of failing the attempt.
    """
    session_id = ""
    config_id = ""
    customer_id = ""
    if isinstance(payload, dict):
        for key, target in (("session_id", "session_id"), ("elements_session_id", "session_id"), ("id", "session_id")):
            value = str(payload.get(key) or "").strip()
            if value.startswith("elements_session_"):
                session_id = value
                break
        else:
            session_id = _find_string_prefix(payload, "elements_session_")
        for key in ("config_id", "elements_config_id", "elements_session_config_id"):
            value = str(payload.get(key) or "").strip()
            if value:
                config_id = value
                break
        if not config_id:
            config_id = _find_string_prefix(payload, "cs_live_") or _find_uuid(payload)
        customer = payload.get("customer")
        if isinstance(customer, dict):
            for key in ("customer", "id"):
                value = str(customer.get(key) or "").strip()
                if value.startswith("cus_"):
                    customer_id = value
                    break
        if not customer_id:
            for key in ("customer", "customer_id"):
                value = str(payload.get(key) or "").strip()
                if value.startswith("cus_"):
                    customer_id = value
                    break
    return session_id, config_id, customer_id


def _find_string_prefix(payload: Any, prefix: str) -> str:
    """Depth-first search for the first string starting with a given prefix."""
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, str) and value.startswith(prefix):
                return value
            found = _find_string_prefix(value, prefix)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_string_prefix(value, prefix)
            if found:
                return found
    return ""


def _find_uuid(payload: Any) -> str:
    pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, str) and pattern.match(value):
                return value
            found = _find_uuid(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_uuid(value)
            if found:
                return found
    return ""


def _is_contract_error(exc: BaseException) -> bool:
    """True when a phase failure is a request-schema/endpoint-level error that a
    different confirm protocol can plausibly avoid (parameter_unknown /
    parameter_missing / endpoint removed / server 5xx).  Account-level declines
    and network failures are NOT contract errors.
    """
    text = str(exc or "").lower()
    if any(marker in text for marker in (
        "parameter_invalid_empty", "parameter_unknown", "parameter_missing",
        "http 404", "http 405", "http 406", "http 500", "http 502", "http 503", "http 504",
        # 2026-09 实测：OpenAI 已将 /backend-api/payments/checkout/confirm 限制为
        # 仅内部 checkout 会话，外部调用一律 400（"only for internal checkout
        # sessions"）。这是端点级限制而非账号/风控拒绝，auto 模式应回退 legacy
        # payment_pages 链路（实测仍可用）而不是直接失败。
        "only for internal checkout sessions", "internal checkout sessions",
    )):
        return True
    return False


class _ProtocolContractError(RuntimeError):
    """Raised when the confirm-token phase hits a schema/endpoint mismatch so
    the auto flow can retry the same attempt over the legacy protocol."""


def _payable_amount(payload: Any) -> int | None:
    """Extract the AUTHORITATIVE payable amount (minor units) from a payment_pages
    payload.  Under a discount the list price stays in amount_total/amount_subtotal
    while the actual charge sits in payment_intent.amount / invoice.amount_due /
    total_summary.due — those are what decide whether the QR is a true 0-yuan link.
    """
    if isinstance(payload, dict):
        payment_intent = payload.get("payment_intent")
        if isinstance(payment_intent, dict):
            for key in ("amount", "amount_received", "amount_capturable"):
                found = _int_amount(payment_intent.get(key))
                if found is not None:
                    return found
        invoice = payload.get("invoice")
        if isinstance(invoice, dict):
            for key in ("amount_due", "amount_remaining", "amount_paid", "total"):
                found = _int_amount(invoice.get(key))
                if found is not None:
                    return found
        total_summary = payload.get("total_summary")
        if isinstance(total_summary, dict):
            for key in ("due", "total"):
                found = _int_amount(total_summary.get(key))
                if found is not None:
                    return found
        for key in ("amount_due", "amount_total", "total", "due"):
            found = _int_amount(payload.get(key))
            if found is not None:
                return found
        for value in payload.values():
            found = _payable_amount(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _payable_amount(value)
            if found is not None:
                return found
    return None


def _pix_qr_amount(qr_data: str) -> int | None:
    """Parse the PIX EMV payload's tag 54 (transaction amount) into BRL minor units.

    A true zero-amount PIX code omits tag 54 entirely (open amount), so None
    means "no amount encoded" (fine for 0-yuan).  A present tag 54 that is not
    zero means the QR charges money — used as an extra guard on top of Stripe.
    """
    text = str(qr_data or "").strip()
    if not text:
        return None
    i = 0
    while i + 4 <= len(text):
        tag = text[i:i + 2]
        try:
            length = int(text[i + 2:i + 4])
        except ValueError:
            break
        value = text[i + 4:i + 4 + length]
        if len(value) < length:
            break
        if tag == "54":
            match = re.fullmatch(r"(\d+)(?:\.(\d{1,2}))?", value)
            if match:
                major = int(match.group(1))
                minor = int((match.group(2) or "")[:2].ljust(2, "0")) if match.group(2) else 0
                return major * 100 + minor
            return None
        i += 4 + length
    return None


def _payment_methods(payload: Any) -> list[str]:
    result: list[str] = []
    if isinstance(payload, dict):
        for key in ("payment_method_types", "ordered_payment_method_types", "ordered_payment_method_types_and_wallets"):
            value = payload.get(key)
            if isinstance(value, list):
                result.extend(str(item).lower() for item in value)
        for key in ("elements_options", "order", "checkout_state"):
            result.extend(_payment_methods(payload.get(key)))
    elif isinstance(payload, list):
        for value in payload:
            if isinstance(value, str):
                result.append(value.lower())
            else:
                result.extend(_payment_methods(value))
    return list(dict.fromkeys(result))


def _extract_pix_action(payload: Any) -> dict[str, Any] | None:
    """Extract Stripe's next_action[pix_display_qr_code] payload."""
    if isinstance(payload, dict):
        next_action = payload.get("next_action")
        if isinstance(next_action, dict):
            inner = next_action.get("pix_display_qr_code")
            if isinstance(inner, dict):
                data = str(inner.get("data") or "").strip()
                png = str(inner.get("image_url_png") or "").strip()
                svg = str(inner.get("image_url_svg") or "").strip()
                hosted = str(inner.get("hosted_instructions_url") or "").strip()
                if hosted or png or svg or data:
                    expires = inner.get("expires_at")
                    try:
                        expires = int(expires)
                    except (TypeError, ValueError):
                        expires = 0
                    return {
                        "hosted_instructions_url": hosted,
                        "image_url_png": png,
                        "image_url_svg": svg,
                        "qr_data": data,
                        "expires_at": expires,
                        # qr_url is an image URL for live UI updates; the
                        # hosted instructions page remains a separate link.
                        "qr_url": png or svg,
                    }
        for value in payload.values():
            found = _extract_pix_action(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _extract_pix_action(value)
            if found:
                return found
    return None


def _extract_link(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("provider_redirect_url", "hosted_instructions_url", "redirect_url", "url", "long_url"):
            value = str(payload.get(key) or "").strip()
            if value.startswith("http://") or value.startswith("https://"):
                return value
        qr = _extract_pix_action(payload)
        if qr and (qr.get("hosted_instructions_url") or qr.get("qr_url")):
            return str(qr.get("hosted_instructions_url") or qr.get("qr_url"))
        for value in payload.values():
            found = _extract_link(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _extract_link(value)
            if found:
                return found
    return ""


def _promo_marker(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False).lower() if isinstance(payload, (dict, list)) else str(payload).lower()
    if "plus-1-month-free" in text and any(marker in text for marker in ("discount_amount", '"total": 0', '"amount_total": 0', '"due": 0')):
        return "applied"
    if any(marker in text for marker in ("not eligible", "ineligible", "invalid_promotion", "already redeemed", "promotion is not available")):
        return "ineligible"
    return "unknown"


def _diagnostic(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload)[:300]
    parts = ["keys=" + ",".join(str(key) for key in list(payload)[:20])]
    submission = payload.get("submission_attempt")
    if isinstance(submission, dict) and submission.get("state"):
        parts.append("state=" + str(submission["state"]))
    # Custom Checkout nests Stripe's decline under
    # submission_attempt.error.payment_error (as in 0元风控.har), rather than
    # exposing it as top-level last_payment_error.
    nested_error = submission.get("error") if isinstance(submission, dict) else None
    if isinstance(nested_error, dict):
        payment_error = nested_error.get("payment_error")
        holders = [nested_error, payment_error] if isinstance(payment_error, dict) else [nested_error]
        for holder in holders:
            code = holder.get("code") or holder.get("decline_code") or holder.get("message")
            if code:
                parts.append("submission_error=" + str(code)[:120])
            decline = holder.get("decline_code")
            if decline and decline != code:
                parts.append("decline_code=" + str(decline)[:120])
    for holder in ("last_payment_error", "error"):
        value = payload.get(holder)
        if isinstance(value, dict):
            code = value.get("code") or value.get("decline_code") or value.get("message")
            if code:
                parts.append(f"{holder}={str(code)[:120]}")
        elif value:
            parts.append(f"{holder}={str(value)[:120]}")
    return "; ".join(parts)


def _upstream_suffix(text: str) -> str:
    """Append the raw upstream payload captured by the raise site (upstream=...)
    so任务详情/日志可以直接看到 OpenAI 的真实返回，无需二次抓包。
    """
    marker = "upstream="
    index = str(text or "").find(marker)
    if index < 0:
        return ""
    tail = str(text)[index + len(marker):].strip()
    if not tail:
        return ""
    if len(tail) > 700:
        tail = tail[:700] + "…"
    return "；上游返回：" + tail


def classify_error(raw: Any, *, stage: str = "") -> tuple[str, bool]:
    text = str(raw or "").lower()
    if any(marker in text for marker in ("pix_billing_tax_id_required", "billing_details[tax_id]", "tax_id required", "cpf/cnpj")):
        return "pix_billing_tax_id_required", False
    if any(marker in text for marker in ("parameter_invalid_empty", "parameter_unknown", "parameter_missing")):
        return "pix_stripe_parameter_error", False
    if "manual approval" in text or "requires_manual_approval" in text or "requires manual approval" in text:
        return "pix_checkout_manual_approval", False
    if "confirm blocked" in text or '"status": "blocked"' in text or '"status":"blocked"' in text:
        return "pix_checkout_blocked", False
    if any(marker in text for marker in ("risk_blocked", "account_review", "payment_blocked", "fraud_blocked", "requires_id")):
        return "pix_risk_blocked", False
    if any(marker in text for marker in ("generic_decline", "payment_failed_generic_decline", "setup_attempt_failed", "payment setup")):
        return "pix_payment_setup_declined", False
    if any(marker in text for marker in ("not eligible", "ineligible", "invalid_promotion", "promotion is not available", "already redeemed")):
        return "pix_promo_ineligible", False
    if "promo_not_applied" in text or "优惠未应用" in text:
        return "pix_promo_not_applied", True
    if "does not offer pix" in text or "pix unavailable" in text:
        return "pix_not_available", False
    if "401" in text or "unauthorized" in text or "access token" in text:
        return "pix_auth_invalid", False
    if "proxy" in text or "timeout" in text or "timed out" in text or "connection" in text or "http 5" in text:
        return "pix_network_error", True
    if stage:
        return f"pix_{stage}_failed", True
    return "pix_extract_failed", True


def localize_pix_error(raw: Any) -> str:
    text = str(raw or "")
    lower = text.lower()
    if "pix_billing_tax_id_required" in lower or "billing_details[tax_id]" in lower or "tax_id required" in lower:
        return "PIX 巴西账单缺少 CPF/CNPJ（tax_id），请在 PIX billing 参数中提供有效税号"
    if "pix_stripe_parameter_error" in lower or any(marker in lower for marker in ("parameter_invalid_empty", "parameter_unknown", "parameter_missing")):
        return "PIX Stripe 请求参数契约错误（不是风控拒绝）：" + text[:1500]
    if "pix_checkout_manual_approval" in lower or "manual approval" in lower or "requires_manual_approval" in lower:
        # 只如实呈现上游返回，不做“人工审核/风控”推断（blocked 的具体原因由上游决定）
        return "PIX Checkout confirm 上游返回 blocked，未生成支付链接/二维码" + _upstream_suffix(text)
    if "pix_checkout_blocked" in lower or "confirm blocked" in lower or '"status": "blocked"' in lower:
        return "PIX Checkout confirm 上游返回 blocked，未生成支付链接/二维码" + _upstream_suffix(text)
    if "pix_risk_blocked" in lower or any(marker in lower for marker in ("risk_blocked", "account_review", "payment_blocked", "fraud_blocked", "requires_id")):
        return "PIX Checkout 被账户/风控状态拦截：上游要求人工处理，未生成支付意图或二维码"
    if "pix_payment_setup_declined" in lower or any(marker in lower for marker in ("generic_decline", "payment_failed_generic_decline", "setup_attempt_failed")):
        return "PIX Checkout 被上游拒付：confirm 返回 generic_decline，未生成有效 PIX 支付意图或二维码"
    if "pix_promo_not_applied" in lower or "promo_not_applied" in lower or "优惠未应用" in text:
        # 保留严格校验发现的具体金额（payable / QR tag54），便于定位是优惠
        # 未生效还是二维码金额异常；纯 eligibility 场景无金额数字。
        amount_detail = ""
        for marker in ("final payable=", "QR tag54 amount=", "amount="):
            if marker in text:
                idx = text.index(marker) + len(marker)
                amount_detail = "（" + text[idx:idx + 20].strip() + "）"
                break
        return "PIX 优惠压0失败：账号没有优惠资格" + amount_detail
    if "pix_promo_ineligible" in lower or any(marker in lower for marker in ("not eligible", "ineligible", "already redeemed")):
        return "PIX 优惠压0失败：账号无优惠资格或优惠已使用"
    if "pix_not_available" in lower or "does not offer pix" in lower:
        return "PIX 支付方式不可用：当前 BR Checkout 未返回 pix"
    if "pix_auth_invalid" in lower or "unauthorized" in lower:
        return "PIX 账号登录凭证已失效，请重新获取 AT"
    if "proxy" in lower or "timeout" in lower or "connection" in lower:
        return "PIX 网络请求失败：请检查代理或稍后重试"
    return "PIX 提链失败：" + text[:1500] if text else "PIX 提链失败：未收到上游返回信息"


class PixHttpClient:
    def __init__(self, proxy: str = "", *, transport: Transport | None = None, fingerprint: dict[str, Any] | None = None):
        self.base_proxy = normalize_proxy(proxy)
        self.proxy = self.base_proxy
        self.transport = transport
        fp = fingerprint if isinstance(fingerprint, dict) else {}
        self.has_fingerprint = bool(fp)
        self.device_id = str(fp.get("device_id") or uuid.uuid4())
        # fingerprint.js emits `oai_session_id`, `ua` and `impersonate`; keep
        # the older aliases for callers that pass a hand-written fixture.
        self.session_id = str(fp.get("oai_session_id") or fp.get("session_seed") or fp.get("session_id") or uuid.uuid4())
        # 未随 Node 指纹档案下发时的兜底默认（部署 curl_cffi 不支持 chrome150 时
        # 自动回退 chrome136），正常链路 Node 端会传完整 --fingerprint 档案。
        self.impersonate = str(fp.get("impersonate") or os.environ.get("PIX_CHROME_PROFILE", "chrome150"))
        self.chrome = self.impersonate  # compatibility for existing callers
        self.ua = str(fp.get("ua") or fp.get("user_agent") or os.environ.get(
            "PIX_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        ))
        self.locale = str(fp.get("locale") or "pt-BR")
        self.timezone = str(fp.get("timezone") or "America/Sao_Paulo")
        self.accept_language = str(fp.get("accept_language") or f"{self.locale},pt;q=0.9")
        self.sec_ch_ua = str(fp.get("sec_ch_ua") or "").strip()
        self.sec_ch_ua_mobile = str(fp.get("sec_ch_ua_mobile") or "?0").strip()
        self.sec_ch_ua_platform = str(fp.get("sec_ch_ua_platform") or '"Windows"').strip()
        self.cookie_header = f"oai-did={self.device_id}"
        self.session_token = str(fp.get("session_token") or os.environ.get("PIX_SESSION_TOKEN") or "").strip()
        self.attestation = ""
        # OpenAI 网页客户端版本头：默认随最新抓包滚动（常量），warmup 探测到
        # 页面内 prod-<40hex> 版本串时自动升级，避免版本陈旧触发风控。
        self.client_version = str(fp.get("client_version") or os.environ.get("PIX_CHATGPT_CLIENT_VERSION") or CHATGPT_CLIENT_VERSION)
        self.client_build = str(fp.get("client_build") or os.environ.get("PIX_CHATGPT_CLIENT_BUILD") or CHATGPT_CLIENT_BUILD)
        self.session = None
        if self.transport is None:
            if curl_requests is None:
                raise RuntimeError("curl_cffi is required for a live PIX extraction")
            try:
                self.session = curl_requests.Session(impersonate=self.impersonate)
            except Exception:
                # A stale persisted target should not create a second session;
                # fall back on the known local default while preserving its jar.
                self.impersonate = os.environ.get("PIX_CHROME_PROFILE", "chrome150")
                self.chrome = self.impersonate
                try:
                    self.session = curl_requests.Session(impersonate=self.impersonate)
                except Exception:
                    self.impersonate = "chrome136"
                    self.chrome = self.impersonate
                    self.session = curl_requests.Session(impersonate=self.impersonate)
            self.session.trust_env = False
            self.session.cookies.set("oai-did", self.device_id, domain="chatgpt.com")
            if self.session_token:
                for name in ("__Secure-next-auth.session-token", "next-auth.session-token"):
                    self.session.cookies.set(name, self.session_token, domain="chatgpt.com")
            self._apply_proxy(self.base_proxy)

    def close(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass

    def _apply_proxy(self, proxy: str = "") -> None:
        self.proxy = normalize_proxy(proxy or self.base_proxy)
        if self.session is not None:
            if self.proxy:
                self.session.proxies.update({"http": self.proxy, "https": self.proxy})
            else:
                self.session.proxies.clear()

    def cookie_header_value(self) -> str:
        """Return the current jar as a Cookie header without losing Set-Cookie."""
        if self.session is not None:
            try:
                values: dict[str, str] = {}
                # curl_cffi's Cookies iterator yields names, so use its
                # domain-aware mapping API instead of assuming Requests'
                # CookieJar iteration shape.
                for domain in ("chatgpt.com", ".chatgpt.com"):
                    try:
                        values.update(self.session.cookies.get_dict(domain=domain))
                    except Exception:
                        pass
                if values:
                    return "; ".join(f"{name}={value}" for name, value in values.items())
            except Exception:
                pass
        return self.cookie_header

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                json_body: dict[str, Any] | None = None, form: dict[str, Any] | None = None,
                timeout: float = 45.0, allow_redirects: bool = False, proxy: str = "") -> tuple[int, str, dict[str, str]]:
        active_proxy = normalize_proxy(proxy or self.base_proxy)
        if self.transport is not None:
            return self.transport(method.upper(), url, headers or {}, json_body, form, timeout, allow_redirects, active_proxy)
        self._apply_proxy(active_proxy)
        response = self.session.request(
            method.upper(), url, headers=dict(headers or {}), json=json_body, data=form,
            timeout=timeout, allow_redirects=allow_redirects, impersonate=self.impersonate,
        )
        return int(response.status_code), response.text or "", {str(k).lower(): str(v) for k, v in response.headers.items()}


def _oai_telemetry() -> str:
    """oai-telemetry 观测形如 [1,551.7,13,37,18,2,0,555]（bax.har）。静态 "[1,null]"
    是明显机器人特征，这里按观测结构生成近似随机值。
    """
    elapsed = round(random.uniform(300.0, 1600.0), 1)
    counter_a = random.randint(2, 60)
    counter_b = random.randint(5, 240)
    counter_c = random.randint(2, 90)
    counter_d = random.randint(80, 4000)
    return f"[1,{elapsed},{counter_a},{counter_b},{counter_c},2,0,{counter_d}]"


def _client_observation() -> str:
    """x-oai-is-client-observation（bax.har 观测：每次请求前新生成，
    形如 v1.r.p.<16 位 base64url>，如 v1.r.p.PwfFG4iWQgfww7We）。

    该值来自页面内 Chromium 部署的客户端观察证明，纯协议无法取得签名值；
    按观测格式随机生成 + env 注入（PIX_CLIENT_OBSERVATION，人工抓包值可复用）
    两条路，PIX_CLIENT_OBSERVATION=off 时不发送该头。
    """
    value = str(os.environ.get("PIX_CLIENT_OBSERVATION") or "").strip()
    if value.lower() == "off":
        return ""
    if value and value.startswith("v1."):
        return value
    token = base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")
    return f"v1.r.p.{token}"


def _chat_headers(client: PixHttpClient, token: str) -> dict[str, str]:
    headers = {
        "user-agent": client.ua,
        "accept": "*/*",
        "accept-language": client.accept_language,
        "authorization": f"Bearer {token}",
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "content-type": "application/json",
        "oai-device-id": client.device_id,
        "oai-language": client.locale,
        "oai-session-id": client.session_id,
        # 版本头陈旧会被上游识别为机器人（放大 manual approval/blocked）；
        # 引擎层支持 warmup 页面探测与 env 覆盖（见 PixHttpClient）。
        "oai-client-version": str(getattr(client, "client_version", "") or CHATGPT_CLIENT_VERSION),
        "oai-client-build-number": str(getattr(client, "client_build", "") or CHATGPT_CLIENT_BUILD),
        "oai-telemetry": _oai_telemetry(),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    account_id = _jwt_account_id(token)
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    if client.attestation:
        headers["oai-web-deployment-attestation"] = client.attestation
    # curl-cffi's session jar is authoritative for live requests.  An
    # injectable transport has no jar, so retain the deterministic cookie
    # header used by fixtures.
    if client.session is None:
        headers["cookie"] = client.cookie_header_value()
    return headers


def _target_headers(base: dict[str, str], path: str, referer: str) -> dict[str, str]:
    headers = dict(base)
    headers["referer"] = referer
    headers["x-openai-target-path"] = path
    headers["x-openai-target-route"] = path
    return headers


def _stripe_headers(client: PixHttpClient) -> dict[str, str]:
    headers = {
        "user-agent": client.ua,
        "accept": "application/json",
        "accept-language": client.accept_language,
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://js.stripe.com",
        "referer": "https://js.stripe.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }
    # Use the same client-hint family as the selected account fingerprint.
    # With no supplied fingerprint, retain the Chrome default used by the CLI.
    sec_ch_ua = client.sec_ch_ua if client.has_fingerprint else (
        '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"'
    )
    if sec_ch_ua:
        headers["sec-ch-ua"] = sec_ch_ua
        headers["sec-ch-ua-mobile"] = client.sec_ch_ua_mobile
        headers["sec-ch-ua-platform"] = client.sec_ch_ua_platform
    return headers


def _fetch_attestation(client: PixHttpClient, url: str) -> str:
    # 对齐 gcash_engine/attestation.py：env 注入（人工抓取值 ~1h）优先，
    # 其次从页面 HTML/RSC payload 抓取 webDeploymentAttestation（部署级签名，
    # 只能从页面 expose 拿，前端 JS 现算不出来）。
    for key in ("PIX_ATTESTATION", "MIN_GCASH_ATTESTATION", "MIN_OAICS_ATTESTATION"):
        value = str(os.environ.get(key) or "").strip()
        if value and "." in value:
            return value
    if client.transport is not None:
        return ""
    try:
        response = client.session.get(url, headers={"user-agent": client.ua, "accept": "text/html,*/*", "referer": url}, timeout=20)
        page_html = str(response.text or "")
        for pattern in (_ATTESTATION_RE, _ATTESTATION_ALT_RE):
            match = pattern.search(page_html)
            if match:
                value = str(match.group(1)).strip()
                if value and "." in value:
                    return value
    except Exception:
        pass
    return ""


_SENTINEL_WARNED = [False]


def _warn_once(message: str) -> None:
    if _SENTINEL_WARNED[0]:
        return
    _SENTINEL_WARNED[0] = True
    try:
        print(f"[pix] {message}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _sentinel_headers(client: PixHttpClient, proxy: str, flow: str, referer: str) -> dict[str, str]:
    """Use the existing Sentinel bridge when available; missing telemetry is diagnostic only."""
    if str(os.environ.get("PIX_SENTINEL", "1")).lower() in {"0", "false", "off"}:
        return {}
    # Injectable transports are deterministic fixtures and must never reach
    # the real Sentinel service.  A captured token can still be supplied via
    # the shared MIN_GCASH_SENTINEL_TOKEN override when explicitly needed.
    if client.transport is not None and not os.environ.get("MIN_GCASH_SENTINEL_TOKEN"):
        return {}
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "gcash_engine", "sentinel_token.py"))
    if not os.path.isfile(path):
        # 服务器部署常见缺失：sentinel mint 依赖 gcash_engine/sentinel_token.py
        # + sentinel_assets/（真 sdk.js 桥），漏传时请求不带 openai-sentinel-token。
        _warn_once(f"sentinel mint unavailable: {path} not found; confirm 请求将不带 openai-sentinel-token（易触发上游 400/blocked）")
        return {}
    try:
        spec = importlib.util.spec_from_file_location("_pix_sentinel", path)
        if not spec or not spec.loader:
            _warn_once(f"sentinel mint unavailable: cannot load {path}")
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        token = module.mint_sentinel_sync(
            flow=flow, device_id=client.device_id, user_agent=client.ua, proxy=proxy,
            page_url=referer, language=client.locale, timezone=client.timezone,
            cores=16, platform="Win32", platform_label="Windows", screen_w=1920,
            screen_h=1080, max_touch_points=0, cookie_header=client.cookie_header_value(), timeout_s=60,
        )
        value = module.sentinel_header_value(token)
        if not value:
            _warn_once("sentinel mint returned no token; confirm 请求将不带 openai-sentinel-token")
        return {"openai-sentinel-token": value} if value else {}
    except Exception as exc:
        _warn_once("sentinel mint failed (" + type(exc).__name__ + "); confirm 请求将不带 openai-sentinel-token")
        return {}


def _custom_fields(*, stripe_js_id: str, elements: dict[str, Any] | None = None, checkout_session_id: str = "", locale: str = "pt-BR") -> dict[str, str]:
    elements = elements if isinstance(elements, dict) else {}
    fields = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": locale,
        "elements_session_client[is_aggregation_expected]": "false",
    }
    # Stripe's initial payment_pages/init request does not include an empty
    # Elements session id.  Sending the key with an empty value is rejected as
    # parameter_invalid_empty; add it only after /v1/elements/sessions returns
    # a real session id for update/confirm/poll.
    elements_session_id = str(elements.get("session_id") or "").strip()
    if elements_session_id:
        fields["elements_session_client[session_id]"] = elements_session_id
    if checkout_session_id:
        fields["client_attribution_metadata[checkout_session_id]"] = checkout_session_id
    return fields


def _elements_options() -> dict[str, str]:
    return {
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }


def _return_url(cs_id: str, processor: str, secret: str = "", hosted_url: str = "") -> str:
    success = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={processor}&plan_type=plus"
    query = {"returned_from_redirect": "true", "ui_mode": "custom", "return_url": success}
    value = ""
    base = f"https://checkout.stripe.com/c/pay/{cs_id}"
    hosted = str(hosted_url or "").strip()
    if hosted:
        parsed = urlsplit(hosted)
        if parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "checkout.stripe.com" and parsed.path:
            base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            value = parsed.fragment
    if not value:
        value = str(secret or "").strip()
        # Custom Checkout encodes the opaque suffix after ``_secret_`` in the
        # fragment.  Keeping it out of the query matches the browser request
        # and avoids double-encoding the already escaped ``%2F`` bytes.
        if "_secret_" in value:
            value = value.split("_secret_", 1)[1]
    if value:
        value = quote(value, safe="%._~-")
    result = f"{base}?{urlencode(query)}"
    return result + (f"#{value}" if value else "")


def _stripe_init(client: PixHttpClient, cs_id: str, stripe_pk: str, stripe_js_id: str, proxy: str, *, country: str = "BR", currency: str = "BRL") -> dict[str, Any]:
    locale = _checkout_locale(country)
    form = {
        "browser_locale": locale,
        "browser_timezone": client.timezone,
        **_custom_fields(stripe_js_id=stripe_js_id, locale=locale),
        **_elements_options(),
        "key": stripe_pk,
        "_stripe_version": STRIPE_CUSTOM_VERSION,
    }
    for _ in range(3):
        status, text, _ = client.request("POST", f"https://api.stripe.com/v1/payment_pages/{cs_id}/init", headers=_stripe_headers(client), form=form, proxy=proxy)
        if status < 400:
            return _json_response(status, text, "PIX Stripe init")
        code, param, _ = _stripe_error_details(text)
        # Only negotiate an explicit malformed/unknown parameter.  A risk or
        # issuer response must pass through unchanged instead of being retried.
        if status == 400 and _is_stripe_parameter_error(code) and _drop_stripe_param(form, param):
            continue
        return _json_response(status, text, "PIX Stripe init")
    raise RuntimeError("PIX Stripe init parameter negotiation exhausted")


def _stripe_elements(client: PixHttpClient, *, stripe_pk: str, stripe_js_id: str, amount: int, cs_id: str, proxy: str, country: str = "BR", currency: str = "BRL", cuss_secret: str = "", token_mode: bool = False) -> dict[str, Any]:
    locale = _checkout_locale(country)
    cur = str(currency or "BRL").strip().lower()
    if token_mode:
        # 新 OA 链路（bax.har 对齐）：Elements 会话由 customer session secret
        # 驱动，页面加载即创建；不携带 checkout_session_id / client_betas。
        params = {
            "deferred_intent[mode]": "subscription",
            "deferred_intent[amount]": str(int(amount)),
            "deferred_intent[currency]": cur,
            "deferred_intent[setup_future_usage]": "off_session",
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[payment_method_types][1]": "link",
            "deferred_intent[payment_method_types][2]": "pix",
            "currency": cur,
            "key": stripe_pk,
            "_stripe_version": STRIPE_BASIL_VERSION,
            "elements_init_source": "stripe.elements",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": stripe_js_id,
            "locale": locale,
            "browser_timezone": client.timezone,
            "type": "deferred_intent",
        }
        if str(cuss_secret or "").strip():
            params["customer_session_client_secret"] = str(cuss_secret).strip()
    else:
        params = {
            "client_betas[0]": "custom_checkout_server_updates_1",
            "client_betas[1]": "custom_checkout_manual_approval_1",
            "deferred_intent[mode]": "subscription",
            "deferred_intent[amount]": str(int(amount)),
            "deferred_intent[currency]": cur,
            "deferred_intent[setup_future_usage]": "off_session",
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[payment_method_types][1]": "link",
            "deferred_intent[payment_method_types][2]": "pix",
            "currency": cur,
            "key": stripe_pk,
            "_stripe_version": STRIPE_CUSTOM_VERSION,
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": stripe_js_id,
            "locale": locale,
            "browser_timezone": client.timezone,
            "type": "deferred_intent",
            "checkout_session_id": cs_id,
        }
    url = "https://api.stripe.com/v1/elements/sessions?" + urlencode(params)
    status, text, _ = client.request("GET", url, headers=_stripe_headers(client), proxy=proxy)
    return _json_response(status, text, "PIX Stripe Elements session")


def _taxes(client: PixHttpClient, token: str, *, cs_id: str, processor: str, billing: dict[str, str], proxy: str, referer: str, country: str = "BR", currency: str = "BRL") -> dict[str, Any]:
    body = {
        "checkout_session_id": cs_id,
        "checkout_email": billing["email"],
        "billing_country": country,
        "billing_name": billing["name"],
        "currency": str(currency or "BRL").strip().lower(),
        "processor_entity": processor,
        "billing_address": {k: billing[k] for k in ("line1", "city", "country", "postal_code", "state")},
    }
    headers = _target_headers(_chat_headers(client, token), "/backend-api/payments/checkout/taxes", referer)
    status, text, _ = client.request("POST", "https://chatgpt.com/backend-api/payments/checkout/taxes", headers=headers, json_body=body, proxy=proxy)
    return _json_response(status, text, "PIX Checkout taxes")


def _snapshot(client: PixHttpClient, token: str, billing: dict[str, str], proxy: str, referer: str) -> None:
    body = {"snapshot": {"billing_address": {"name": billing["name"], "address": {k: billing[k] for k in ("line1", "city", "country", "postal_code", "state")}}}}
    headers = _target_headers(_chat_headers(client, token), "/backend-api/payments/checkout/snapshot", referer)
    status, _, _ = client.request("POST", "https://chatgpt.com/backend-api/payments/checkout/snapshot", headers=headers, json_body=body, proxy=proxy)
    if status not in (200, 204):
        raise RuntimeError(f"PIX billing snapshot HTTP {status}")


def _stripe_update(client: PixHttpClient, *, cs_id: str, stripe_pk: str, stripe_js_id: str, elements: dict[str, Any], billing: dict[str, str], proxy: str, country: str = "BR") -> dict[str, Any]:
    locale = _checkout_locale(country)
    form = {
        "tax_region[country]": country,
        "tax_region[line1]": billing["line1"],
        "tax_region[city]": billing["city"],
        "tax_region[postal_code]": billing["postal_code"],
        "tax_region[state]": billing["state"],
        **_custom_fields(stripe_js_id=stripe_js_id, elements=elements, checkout_session_id=cs_id, locale=locale),
        **_elements_options(),
        "key": stripe_pk,
        "_stripe_version": STRIPE_CUSTOM_VERSION,
    }
    status, text, _ = client.request("POST", f"https://api.stripe.com/v1/payment_pages/{cs_id}", headers=_stripe_headers(client), form=form, proxy=proxy)
    return _json_response(status, text, "PIX Stripe tax update")


def _confirm_form(*, cs_id: str, processor: str, stripe_pk: str, stripe_js_id: str,
                  elements: dict[str, Any], init_data: dict[str, Any], billing: dict[str, str],
                  amount: int, checkout_secret: str, hosted_url: str = "",
                  checkout_config_id: str = "", country: str = "BR") -> dict[str, str]:
    config_id = str(init_data.get("config_id") or "")
    elements_id = str(elements.get("session_id") or "")
    elements_config = str(elements.get("config_id") or "")
    locale = _checkout_locale(country)
    form: dict[str, str] = {
        "guid": str(uuid.uuid4()), "muid": str(uuid.uuid4()), "sid": str(uuid.uuid4()),
        "expected_amount": str(int(amount)), "expected_payment_method_type": "pix",
        "return_url": _return_url(cs_id, processor, checkout_secret, hosted_url),
        "init_checksum": str(init_data.get("init_checksum") or ""),
        "version": str(init_data.get("version") or STRIPE_RUNTIME_VERSION),
        "key": stripe_pk, "_stripe_version": STRIPE_CUSTOM_VERSION,
        "payment_method_data[type]": "pix",
        "payment_method_data[billing_details][name]": billing["name"],
        "payment_method_data[billing_details][email]": billing["email"],
        "payment_method_data[billing_details][address][line1]": billing["line1"],
        "payment_method_data[billing_details][address][city]": billing["city"],
        "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
        "payment_method_data[billing_details][address][state]": billing["state"],
        "payment_method_data[billing_details][address][country]": country,
        "payment_method_data[payment_user_agent]": f"stripe.js/{STRIPE_RUNTIME_VERSION}; stripe-js-v3/{STRIPE_RUNTIME_VERSION}; payment-element; deferred-intent",
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(25000, 55000)),
        "link_brand": "link",
        # Custom Checkout sends the Elements client context on confirm as
        # well as on init/update.  Stripe uses these fields to bind the
        # payment method to the same Elements session and checkout config.
        **_custom_fields(stripe_js_id=stripe_js_id, elements=elements, checkout_session_id=cs_id, locale=locale),
        **_elements_options(),
    }
    tax_id = str(billing.get("tax_id") or "").strip()
    if tax_id:
        form["payment_method_data[billing_details][tax_id]"] = tax_id
    # The browser sends attribution inside the payment method and at root.
    attrs = {
        "client_session_id": stripe_js_id,
        "checkout_session_id": cs_id,
        "merchant_integration_source": "elements",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "2021",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
        "elements_session_id": elements_id,
        "elements_session_config_id": elements_config,
        "checkout_config_id": config_id,
    }
    for key, value in attrs.items():
        form[f"payment_method_data[client_attribution_metadata][{key}]"] = value
    # Root attribution belongs to the Checkout page, whose config ID may
    # advance after the billing/tax update.  The nested attribution remains
    # bound to the original Elements init config above.
    root_attrs = dict(attrs)
    root_attrs["merchant_integration_source"] = "checkout"
    root_attrs["merchant_integration_version"] = "custom"
    root_attrs["checkout_config_id"] = str(checkout_config_id or config_id)
    for key, value in root_attrs.items():
        form[f"client_attribution_metadata[{key}]"] = value
    for index, name in enumerate(("expressCheckout", "payment", "address")):
        form[f"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][{index}]"] = name
        form[f"client_attribution_metadata[merchant_integration_additional_elements][{index}]"] = name
    # These values are produced by Stripe's browser runtime.  Keep them
    # optional and accept env injection for browser-captured tokens.
    for env_key, form_key in (("PIX_JS_CHECKSUM", "js_checksum"), ("PIX_RV_TIMESTAMP", "rv_timestamp"), ("PIX_PASSIVE_CAPTCHA_TOKEN", "passive_captcha_token"), ("PIX_PASSIVE_CAPTCHA_EKEY", "passive_captcha_ekey")):
        value = str(os.environ.get(env_key) or "").strip()
        if value:
            form[form_key] = value
    # The browser includes this key even when the optional ekey is empty.
    form.setdefault("passive_captcha_ekey", "")
    return form


def _stripe_confirm(client: PixHttpClient, *, cs_id: str, processor: str, stripe_pk: str, stripe_js_id: str,
                    elements: dict[str, Any], init_data: dict[str, Any], billing: dict[str, str],
                    amount: int, checkout_secret: str, proxy: str, checkout_config_id: str = "", country: str = "BR") -> dict[str, Any]:
    form = _confirm_form(cs_id=cs_id, processor=processor, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id, elements=elements, init_data=init_data, billing=billing, amount=amount, checkout_secret=checkout_secret, hosted_url=str(init_data.get("stripe_hosted_url") or ""), checkout_config_id=checkout_config_id, country=country)
    for _ in range(3):
        status, text, _ = client.request("POST", f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm", headers=_stripe_headers(client), form=form, proxy=proxy)
        if status == 200:
            return _json_response(status, text, "PIX Stripe confirm")
        code, param, _ = _stripe_error_details(text)
        if status == 400 and _is_stripe_parameter_error(code) and _drop_stripe_param(form, param):
            continue
        return _json_response(status, text, "PIX Stripe confirm")
    raise RuntimeError("PIX Stripe confirm parameter negotiation exhausted")


def _confirmation_token_form(*, stripe_pk: str, stripe_js_id: str, elements_session_id: str,
                          elements_config_id: str, config_id: str, billing: dict[str, str],
                          currency: str = "brl", customer_id: str = "", amount: int = 0) -> dict[str, str]:
    """Build the /v1/confirmation_tokens form exactly as chatgpt.com's
    Payment Element does (bax.har 2026-09-08): the PIX payment method plus
    billing details (including tax_id and an empty phone), Elements session
    binding and both attribution scopes.
    """
    session_id = str(elements_session_id or "").strip()
    config_id_value = str(elements_config_id or config_id or "").strip()
    tax_id = str(billing.get("tax_id") or "").strip()
    form: dict[str, str] = {
        "payment_method_data[type]": "pix",
        "payment_method_data[billing_details][name]": billing["name"],
        "payment_method_data[billing_details][email]": billing["email"],
        "payment_method_data[billing_details][address][line1]": billing["line1"],
        "payment_method_data[billing_details][address][city]": billing["city"],
        "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
        "payment_method_data[billing_details][address][state]": billing["state"],
        "payment_method_data[billing_details][address][country]": billing["country"],
        "payment_method_data[billing_details][phone]": "",
        "payment_method_data[payment_user_agent]": (
            f"stripe.js/{STRIPE_JS_REVISION}; stripe-js-v3/{STRIPE_JS_REVISION}; "
            "payment-element; deferred-intent"
        ),
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(25000, 120000)),
        "setup_future_usage": "off_session",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
        "client_context[currency]": str(currency or "brl").strip().lower(),
        "client_context[mode]": "subscription",
        "client_context[payment_method_types][0]": "card",
        "client_context[payment_method_types][1]": "link",
        "client_context[payment_method_types][2]": "pix",
        "set_as_default_payment_method": "false",
        "key": stripe_pk,
        "_stripe_version": STRIPE_BASIL_VERSION,
    }
    if tax_id:
        form["payment_method_data[billing_details][tax_id]"] = tax_id
    if str(customer_id or "").strip():
        form["client_context[customer]"] = str(customer_id).strip()
    # Stripe's browser runtime ids (m.stripe.com/6) — random per attempt.
    form["payment_method_data[guid]"] = str(uuid.uuid4())
    form["payment_method_data[muid]"] = str(uuid.uuid4())
    form["payment_method_data[sid]"] = str(uuid.uuid4())
    hcaptcha = str(os.environ.get("PIX_HCAPTCHA_TOKEN") or os.environ.get("MIN_GCASH_HCAPTCHA_TOKEN") or "").strip()
    if hcaptcha:
        form["payment_method_data[radar_options][hcaptcha_token]"] = hcaptcha
    attrs = {
        "client_session_id": stripe_js_id,
        "merchant_integration_source": "elements",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "2021",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
    }
    if session_id:
        attrs["elements_session_id"] = session_id
    if config_id_value:
        attrs["elements_session_config_id"] = config_id_value
    for key, value in attrs.items():
        form[f"payment_method_data[client_attribution_metadata][{key}]"] = value
        form[f"client_attribution_metadata[{key}]"] = value
    for index, name in enumerate(("expressCheckout", "payment", "address")):
        form[f"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][{index}]"] = name
        form[f"client_attribution_metadata[merchant_integration_additional_elements][{index}]"] = name
    return form


def _stripe_confirmation_token(client: PixHttpClient, *, stripe_pk: str, stripe_js_id: str,
                               elements: dict[str, Any], config_id: str, billing: dict[str, str],
                               proxy: str, currency: str = "brl", customer_id: str = "") -> dict[str, Any]:
    """POST api.stripe.com/v1/confirmation_tokens (bax.har 对齐).

    Returns the parsed confirmation token; the id (ctoken_...) is what the
    OpenAI confirm endpoint expects.  Unknown-parameter 400s are negotiated by
    dropping the offending field, exactly like the legacy init/confirm path.
    """
    session_id, elements_config, _ = _elements_session_parts(elements)
    form = _confirmation_token_form(
        stripe_pk=stripe_pk, stripe_js_id=stripe_js_id,
        elements_session_id=session_id or str(elements.get("session_id") or ""),
        elements_config_id=elements_config or str(elements.get("config_id") or ""),
        config_id=config_id, billing=billing, currency=currency, customer_id=customer_id,
    )
    url = "https://api.stripe.com/v1/confirmation_tokens"
    for _ in range(3):
        status, text, _ = client.request("POST", url, headers=_stripe_headers(client), form=form, proxy=proxy)
        if status < 400:
            return _json_response(status, text, "PIX Stripe confirmation token")
        code, param, _ = _stripe_error_details(text)
        if status == 400 and _is_stripe_parameter_error(code) and _drop_stripe_param(form, param):
            continue
        return _json_response(status, text, "PIX Stripe confirmation token")
    raise _ProtocolContractError("PIX confirmation token parameter negotiation exhausted")


def _oa_confirm(client: PixHttpClient, token: str, *, cs_id: str, processor: str, ctoken: str,
                proxy: str, referer: str, manual_approval: bool = False) -> dict[str, Any]:
    """POST /backend-api/payments/checkout/confirm with the Stripe confirmation
    token (bax.har 对齐).  The OpenAI backend performs the actual confirmation
    and answers with {"status": ...}; "blocked" is terminal here.
    """
    headers = _target_headers(_chat_headers(client, token), "/backend-api/payments/checkout/confirm", referer)
    observation = _client_observation()
    if observation:
        headers["x-oai-is-client-observation"] = observation
    headers.update(_sentinel_headers(client, proxy, "checkout_session_approval", referer))
    body = {
        "checkout_session_id": cs_id,
        "confirm_token": ctoken,
        "selected_payment_method_type": "pix",
    }
    status, text, _ = client.request("POST", "https://chatgpt.com/backend-api/payments/checkout/confirm",
                                     headers=headers, json_body=body, proxy=proxy)
    payload = _json_response(status, text, "PIX Checkout confirm")
    state = str((payload.get("submission_attempt") or {}).get("state") or payload.get("status") or "").strip().lower()
    if state in {"blocked", "risk_blocked", "fraud_blocked"}:
        # 把上游 confirm 的真实响应原文一起带出（截断到 600），任务详情/服务端
        # 日志可以直接看到 {"status":"blocked"} 之类的原始返回，无需二次抓包。
        upstream = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:600]
        if manual_approval or "manual_approval" in str(payload.get("error") or "").lower():
            raise RuntimeError(f"PIX OA confirm blocked by upstream: status=blocked; account requires manual approval; upstream={upstream}")
        raise RuntimeError(f"PIX OA confirm blocked by upstream: status=blocked; upstream={upstream}")
    return payload


def _confirm_phase_legacy(client: PixHttpClient, token: str, billing: dict[str, str], *,
                          cs_id: str, processor: str, stripe_pk: str, checkout_secret: str,
                          checkout_payload: dict[str, Any], referer: str,
                          require_zero: bool, force_payment_method: bool,
                          force_zero_checkout: bool, max_poll_attempts: int,
                          country: str, currency: str, on_step: Any, on_qr_update: Any,
                          sleep_fn: Callable[[float], None]) -> tuple[dict[str, Any], dict[str, Any], int]:
    """原有 payment_pages init/confirm/approve/poll 链路（0元风控.har 时代协议）。

    Returns (payment payload, elements, final amount) — amount keeps the
    legacy evolution (init amount -> taxes amount).
    """
    def step(name: str, status: str, detail: str) -> None:
        if callable(on_step):
            on_step({"name": name, "status": status, "detail": str(detail)[:800]})

    stripe_js_id = str(uuid.uuid4())
    init_data = _stripe_init(client, cs_id, stripe_pk, stripe_js_id, client.base_proxy, country=country, currency=currency)
    amount = _amount(init_data)
    if amount is None:
        amount = _checkout_total_minor(checkout_payload)
    if amount is None:
        amount = 0
    step("stripe init", "ok", f"amount={amount}; config={str(init_data.get('config_id') or '')[:16]}")
    elements = _stripe_elements(client, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id, amount=amount, cs_id=cs_id, proxy=client.base_proxy, country=country, currency=currency)
    if (not force_payment_method and
            "pix" not in _payment_methods(elements) and
            "pix" not in _payment_methods(init_data)):
        raise RuntimeError("PIX Checkout does not offer pix")
    step("stripe elements", "ok", f"session={str(elements.get('session_id') or '')[:20]}; pix offered")
    taxes = _taxes(client, token, cs_id=cs_id, processor=processor, billing=billing, proxy=client.base_proxy, referer=referer, country=country, currency=currency)
    tax_amount = _amount(taxes)
    if tax_amount is not None:
        amount = tax_amount
    step("taxes", "ok", f"country={country}; amount={amount}")
    if require_zero and not force_zero_checkout and amount != 0:
        raise RuntimeError(f"PIX promo_not_applied: amount={amount}")
    _snapshot(client, token, billing, client.base_proxy, referer)
    step("billing snapshot", "ok", "billing snapshot submitted")
    checkout_config_id = ""
    try:
        update_data = _stripe_update(client, cs_id=cs_id, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id, elements=elements, billing=billing, proxy=client.base_proxy, country=country)
        checkout_config_id = _first(update_data, ("config_id", "checkout_config_id"))
        step("stripe tax update", "ok", f"tax region={country}")
    except Exception as exc:
        step("stripe tax update", "warn", f"{type(exc).__name__}: non-blocking")
    confirmation = _stripe_confirm(client, cs_id=cs_id, processor=processor, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id, elements=elements, init_data=init_data, billing=billing, amount=amount, checkout_secret=checkout_secret, proxy=client.base_proxy, checkout_config_id=checkout_config_id, country=country)
    qr = _extract_pix_action(confirmation)
    step("stripe confirm", "ok", _diagnostic(confirmation))
    if qr and callable(on_qr_update):
        on_qr_update({**qr, "event": "qr_refresh", "qr_url": qr.get("qr_url") or "", "updated_at": time.time()})
    submission = confirmation.get("submission_attempt") if isinstance(confirmation, dict) else None
    if isinstance(submission, dict) and str(submission.get("state") or "").lower() == "failed":
        raise RuntimeError("PIX Custom Checkout submission failed: " + _diagnostic(confirmation))
    if str(submission.get("state") or "").lower() == "requires_approval" if isinstance(submission, dict) else False:
        _approve(client, token, cs_id=cs_id, processor=processor, proxy=client.base_proxy, referer=referer)
        step("checkout approve", "ok", "manual approval submitted")
    payment = confirmation if (qr or _extract_link(confirmation)) else _poll(client, cs_id=cs_id, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id, elements=elements, proxy=client.base_proxy, max_attempts=max_poll_attempts, on_qr_update=on_qr_update, sleep_fn=sleep_fn)
    return payment, elements, amount


def _confirm_phase_token(client: PixHttpClient, token: str, billing: dict[str, str], *,
                         cs_id: str, processor: str, stripe_pk: str, checkout_payload: dict[str, Any],
                         cuss_secret: str, manual_approval: bool, referer: str,
                         require_zero: bool, force_payment_method: bool, force_zero_checkout: bool,
                         max_poll_attempts: int, country: str, currency: str,
                         on_step: Any, on_qr_update: Any, sleep_fn: Callable[[float], None]) -> tuple[dict[str, Any], dict[str, Any], int]:
    """新 OA 链路（bax.har 对齐）：elements/sessions(cuss) → taxes →
    confirmation_tokens → /payments/checkout/confirm(JSON)。

    Returns (payment payload, elements, final amount).  Confirm response
    shapes handled:
      * next_action pix QR (paid flow) — returned as-is,
      * status succeeded/complete — returned as-is,
      * anything else — falls back to polling payment_pages for the QR/state.
    """
    def step(name: str, status: str, detail: str) -> None:
        if callable(on_step):
            on_step({"name": name, "status": status, "detail": str(detail)[:800]})

    amount_hint = _checkout_total_minor(checkout_payload)
    if amount_hint is None:
        amount_hint = 0
    stripe_js_id = str(uuid.uuid4())
    try:
        elements = _stripe_elements(client, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id, amount=amount_hint, cs_id=cs_id, proxy=client.base_proxy, country=country, currency=currency, cuss_secret=cuss_secret, token_mode=True)
    except Exception as exc:
        if _is_contract_error(exc):
            raise _ProtocolContractError(f"elements sessions (token): {exc}") from exc
        raise
    if (not force_payment_method and
            "pix" not in _payment_methods(elements) and
            "pix" not in _payment_methods(checkout_payload)):
        raise RuntimeError("PIX Checkout does not offer pix")
    step("stripe elements", "ok", f"session={str(elements.get('session_id') or '')[:20]}; pix offered")
    try:
        taxes = _taxes(client, token, cs_id=cs_id, processor=processor, billing=billing, proxy=client.base_proxy, referer=referer, country=country, currency=currency)
    except Exception as exc:
        if _is_contract_error(exc):
            raise _ProtocolContractError(f"taxes (token): {exc}") from exc
        raise
    amount = _amount(taxes)
    if amount is None:
        amount = amount_hint
    step("taxes", "ok", f"country={country}; amount={amount}")
    if require_zero and not force_zero_checkout and amount != 0:
        raise RuntimeError(f"PIX promo_not_applied: amount={amount}")
    _, _, customer_id = _elements_session_parts(elements)
    if not customer_id:
        customer_id = _find_string_prefix(taxes, "cus_") if isinstance(taxes, dict) else ""
    try:
        ctoken_payload = _stripe_confirmation_token(
            client, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id, elements=elements,
            config_id=str(elements.get("config_id") or ""), billing=billing,
            proxy=client.base_proxy, currency=currency, customer_id=customer_id,
        )
    except Exception as exc:
        if _is_contract_error(exc):
            raise _ProtocolContractError(f"confirmation_tokens (token): {exc}") from exc
        raise
    ctoken = str(ctoken_payload.get("id") or "").strip()
    if not ctoken:
        raise _ProtocolContractError(f"confirmation token id missing: {_diagnostic(ctoken_payload)}")
    step("stripe ctoken", "ok", f"token={ctoken[:22]}…; pm=pix")
    try:
        confirmation = _oa_confirm(
            client, token, cs_id=cs_id, processor=processor, ctoken=ctoken,
            proxy=client.base_proxy, referer=referer, manual_approval=manual_approval,
        )
    except Exception as exc:
        # blocked 等业务终态照常抛；只有 schema/端点错误允许回退 legacy。
        if _is_contract_error(exc):
            raise _ProtocolContractError(f"checkout confirm (token): {exc}") from exc
        raise
    qr = _extract_pix_action(confirmation)
    if qr and callable(on_qr_update):
        on_qr_update({**qr, "event": "qr_refresh", "qr_url": qr.get("qr_url") or "", "updated_at": time.time()})
    state = str((confirmation.get("submission_attempt") or {}).get("state") or confirmation.get("status") or "").strip().lower()
    step("oa confirm", "ok", f"status={state}; {_diagnostic(confirmation)[:220]}")
    if qr or _extract_link(confirmation) or state in {"succeeded", "complete", "approved", "confirmed"}:
        return confirmation, elements, amount
    if state in {"open", "pending", "processing", "requires_action", "requires_approval", ""}:
        try:
            payment = _poll(client, cs_id=cs_id, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id, elements=elements, proxy=client.base_proxy, max_attempts=max_poll_attempts, on_qr_update=on_qr_update, sleep_fn=sleep_fn)
        except Exception as exc:
            # payment_pages 轮询在新协议下可能已下线（404）→ 视为契约错误以触发回退
            if _is_contract_error(exc):
                raise _ProtocolContractError(f"payment page poll (token): {exc}") from exc
            raise
        state = str((payment.get("submission_attempt") or {}).get("state") or payment.get("status") or "").strip().lower()
        if _extract_pix_action(payment) or _extract_link(payment) or state in {"succeeded", "complete", "approved"}:
            return payment, elements, amount
        raise RuntimeError(f"PIX OA confirm did not produce a payment result: status={state}; {_diagnostic(payment)[:220]}")
    raise RuntimeError(f"PIX OA confirm unexpected status: {state}; {_diagnostic(confirmation)[:220]}")


def _approve(client: PixHttpClient, token: str, *, cs_id: str, processor: str, proxy: str, referer: str) -> dict[str, Any]:
    for attempt in range(2):
        headers = _target_headers(_chat_headers(client, token), "/backend-api/payments/checkout/approve", referer)
        observation = _client_observation()
        if observation:
            headers["x-oai-is-client-observation"] = observation
        headers.update(_sentinel_headers(client, proxy, "checkout_session_approval", referer))
        status, text, _ = client.request("POST", "https://chatgpt.com/backend-api/payments/checkout/approve", headers=headers, json_body={"checkout_session_id": cs_id, "processor_entity": processor}, proxy=proxy)
        data = _json_response(status, text, "PIX Checkout approve")
        result = str(data.get("result") or "").lower()
        if result == "approved":
            return data
        if attempt == 0 and result in {"blocked", "expired", "risk_blocked"}:
            continue
        raise RuntimeError(f"PIX Checkout approve rejected: {result or 'unknown'}; {_diagnostic(data)}")
    raise RuntimeError("PIX Checkout approve retry exhausted")


def _poll(client: PixHttpClient, *, cs_id: str, stripe_pk: str, stripe_js_id: str, elements: dict[str, Any], proxy: str,
          max_attempts: int, on_qr_update: Any = None, sleep_fn: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    params = {
        "key": stripe_pk, "_stripe_version": STRIPE_CUSTOM_VERSION,
        **_custom_fields(stripe_js_id=stripe_js_id, elements=elements, checkout_session_id=cs_id),
        **_elements_options(),
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}?" + urlencode({k: v for k, v in params.items() if not k.startswith("client_attribution_metadata")})
    last: dict[str, Any] = {}
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        if attempt == 1 and max_attempts > 1:
            sleep_fn(1.5)
        status, text, _ = client.request("GET", url, headers=_stripe_headers(client), proxy=proxy)
        if status == 400 and "checkout_not_active_session" in (text or "") and attempt < max_attempts:
            sleep_fn(1.5)
            continue
        last = _json_response(status, text, "PIX Stripe payment page poll")
        qr = _extract_pix_action(last)
        if qr:
            payload = {**qr, "event": "qr_refresh", "qr_url": qr.get("qr_url") or "", "updated_at": time.time()}
            if callable(on_qr_update):
                on_qr_update(payload)
        link = _extract_link(last)
        submission = last.get("submission_attempt") if isinstance(last, dict) else None
        state = str(submission.get("state") or "").lower() if isinstance(submission, dict) else str(last.get("status") or "").lower()
        if state == "failed":
            raise RuntimeError("PIX Custom Checkout submission failed: " + _diagnostic(last))
        if link or qr or state in {"succeeded", "complete"}:
            return last
        if attempt < max_attempts:
            sleep_fn(1.5)
    return last


def _has_active_plus(value: Any) -> bool:
    """Recursively check a subscriptions payload for an active Plus plan."""
    if isinstance(value, dict):
        plan = str(value.get("plan_type") or "").strip().lower()
        if plan == "plus" and value.get("is_delinquent") is not True:
            try:
                return int(value.get("seats_entitled", 1) or 0) >= 1
            except (TypeError, ValueError):
                return True
        return any(_has_active_plus(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_active_plus(item) for item in value)
    return False


def _subscriptions_active(client: PixHttpClient, token: str, proxy: str) -> bool:
    """Poll /backend-api/subscriptions for an active Plus plan (GCash-aligned)."""
    account_id = _jwt_account_id(token)
    if not account_id:
        return False
    headers = _target_headers(_chat_headers(client, token), "/backend-api/subscriptions", "https://chatgpt.com/")
    headers["Chatgpt-Account-Id"] = account_id
    try:
        status, text, _ = client.request(
            "GET",
            "https://chatgpt.com/backend-api/subscriptions?" + urlencode({"account_id": account_id}),
            headers=headers,
            proxy=proxy,
        )
        if status >= 400:
            return False
        try:
            return _has_active_plus(json.loads(text or "{}"))
        except json.JSONDecodeError:
            return False
    except Exception:
        return False


def _start_paid_watch(client: PixHttpClient, token: str, *, cs_id: str, stripe_pk: str, stripe_js_id: str,
                        elements: dict[str, Any], proxy: str, timeout_seconds: float,
                        on_step: Any = None, on_paid: Any = None,
                        sleep_fn: Callable[[float], None] = time.sleep) -> threading.Thread:
    """Start a non-daemon keep-alive thread that watches for the payment landing.

    Aligned with GCash: the QR/link is handed off immediately while this thread
    keeps the SAME client session alive, polling Stripe submission state and
    subscriptions until the payment is confirmed (or the timeout).  Non-daemon
    keeps the CLI process alive so Node can keep receiving [paid]/payment_status
    events, exactly like the GCash QR browser session.
    """

    def _watch() -> None:
        try:
            _wait_paid(
                client, token, cs_id=cs_id, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id,
                elements=elements, proxy=proxy, timeout_seconds=timeout_seconds,
                on_step=on_step, on_paid=on_paid, sleep_fn=sleep_fn,
            )
        finally:
            try:
                client.close()
            except Exception:
                pass

    thread = threading.Thread(target=_watch, name="pix-paid-watch", daemon=False)
    thread.start()
    return thread


def _wait_paid(client: PixHttpClient, token: str, *, cs_id: str, stripe_pk: str, stripe_js_id: str,
               elements: dict[str, Any], proxy: str, timeout_seconds: float,
               on_step: Any = None, on_paid: Any = None, sleep_fn: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Keep-alive payment confirmation, aligned with GCash's browser session.

    After the QR/link is handed off we stay in the SAME client session
    (same device/session/cookie/proxy identity) and poll until Stripe reports
    the submission succeeded/complete, then confirm Plus via subscriptions.
    This is what makes the payment actually land on the account — without it
    the caller only gets a QR that may never be reconciled.
    """
    def step(name: str, status: str, detail: str) -> None:
        if callable(on_step):
            on_step({"name": name, "status": status, "detail": str(detail)[:800]})

    params = {
        "key": stripe_pk, "_stripe_version": STRIPE_CUSTOM_VERSION,
        **_custom_fields(stripe_js_id=stripe_js_id, elements=elements, checkout_session_id=cs_id),
        **_elements_options(),
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}?" + urlencode({k: v for k, v in params.items() if not k.startswith("client_attribution_metadata")})
    deadline = time.monotonic() + max(30.0, float(timeout_seconds))
    last_state = ""
    step("paid-watch", "run", f"keeping the same session alive; watching payment for up to {int(timeout_seconds)}s")
    while time.monotonic() < deadline:
        if _subscriptions_active(client, token, proxy):
            step("paid-watch", "ok", "Plus subscription is active; payment confirmed")
            if callable(on_paid):
                on_paid("plus_active")
            return {"paid": True, "payment_status": "succeeded", "confirmed_via": "subscriptions"}
        try:
            status, text, _ = client.request("GET", url, headers=_stripe_headers(client), proxy=proxy)
            if status >= 400:
                sleep_fn(3.0)
                continue
            payload = _json_response(status, text, "PIX Stripe paid poll")
            submission = payload.get("submission_attempt") if isinstance(payload, dict) else None
            state = str(submission.get("state") or "").lower() if isinstance(submission, dict) else str(payload.get("status") or "").lower()
            if state != last_state and state:
                last_state = state
                step("paid-watch", "run", f"submission state={state}")
            if state in {"succeeded", "complete"}:
                step("paid-watch", "ok", "Stripe submission succeeded; payment landed")
                if callable(on_paid):
                    on_paid("plus_active")
                return {"paid": True, "payment_status": state, "confirmed_via": "payment_pages"}
        except Exception:
            pass
        sleep_fn(3.0)
    step("paid-watch", "warn", "payment watch timed out; QR still valid for manual payment")
    return {"paid": False, "payment_status": last_state or "pending", "confirmed_via": "timeout"}


def _load_billing_profiles() -> list[dict[str, str]]:
    """Load a user-maintained pool of REAL Brazilian billing profiles.

    Random CNPJs from the built-in pool are registered but instantly
    recognisable (major corporates); a production setup should feed its own
    verified profiles here.  JSON array file, each entry:
    {"name","tax_id","line1","city","state","postal_code","tax_kind"?,"email"?}
    Env PIX_BILLING_PROFILES overrides the default data/pix_billing_profiles.json.
    """
    try:
        candidates = [
            str(os.environ.get("PIX_BILLING_PROFILES") or "").strip(),
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "pix_billing_profiles.json"),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            if not os.path.isfile(candidate):
                continue
            with open(candidate, "r", encoding="utf-8") as handle:
                value = json.loads(handle.read())
            entries = value if isinstance(value, list) else value.get("profiles") if isinstance(value, dict) else None
            if not isinstance(entries, list):
                continue
            profiles = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                profile = {key: str(entry.get(key) or "").strip() for key in ("name", "tax_id", "line1", "city", "state", "postal_code", "tax_kind", "email")}
                # 地址字段缺失会在 taxes/confirm 阶段制造 400，入池前校验完整
                if profile.get("tax_id") and profile.get("name") and profile.get("line1") and profile.get("city") and profile.get("state") and profile.get("postal_code"):
                    profiles.append(profile)
            if profiles:
                return profiles
    except Exception:
        pass
    return []


def _normalize_billing(billing: dict[str, Any] | None, email: str, country: str = "BR") -> dict[str, str]:
    source = billing if isinstance(billing, dict) else {}
    address = source.get("address") if isinstance(source.get("address"), dict) else source
    provided = {
        key: (source.get(key) if key in source else address.get(key))
        for key in ("name", "email", "line1", "city", "state", "postal_code", "tax_id")
    }
    cc = str(country or "BR").strip().upper() or "BR"
    if cc == "BR":
        # 优先用户维护的真实账单素材池（data/pix_billing_profiles.json），
        # 每次尝试从中随机抽一套；无素材池时回落到内置真实注册 CNPJ 池。
        # 显式 billing 参数始终优先于池内随机。
        source_pool = _load_billing_profiles()
        if source_pool:
            picked = random.choice(source_pool)
            profile = {
                "name": picked.get("name") or "",
                "email": picked.get("email") or email or str(provided.get("email") or ""),
                "line1": picked.get("line1") or "",
                "city": picked.get("city") or "",
                "state": picked.get("state") or "",
                "postal_code": picked.get("postal_code") or "",
                "tax_id": picked.get("tax_id") or "",
                "tax_kind": picked.get("tax_kind") or "",
                "country": "BR",
            }
        else:
            # A fresh random BR profile supplies every field that the caller left
            # empty: a REAL registered CNPJ (default) or a checksum-valid CPF,
            # plus a realistic name and address.
            profile = random_brazil_billing(
                email=email or str(provided.get("email") or ""),
                tax_kind=str(source.get("tax_kind") or address.get("tax_kind") or ""),
            )
    else:
        # PIX 是巴西支付方式，CPF/CNPJ 仅巴西适用。自由选择其他建单国家时
        # 不生成巴西税号、也不填巴西地址；姓名从邮箱 local-part 派生，账单
        # 其余字段交由调用方（前端 billing 参数）提供，避免硬编码任何国家。
        name = _name_from_email(email or str(provided.get("email") or ""))
        profile = {
            "name": name,
            "email": email or str(provided.get("email") or ""),
            "line1": "", "city": "", "state": "", "postal_code": "",
            "country": cc, "tax_id": "",
        }
    result: dict[str, str] = {}
    for key in ("name", "email", "line1", "city", "state", "postal_code"):
        value = provided.get(key)
        result[key] = str(value).strip() if value not in (None, "") else profile[key]
    tax_id = str(provided.get("tax_id") or "").strip()
    if not tax_id:
        tax_id = str(os.environ.get("PIX_TAX_ID") or "").strip()
    if not tax_id and cc == "BR":
        # Defaults to a real registered CNPJ; never a fabricated placeholder.
        tax_id = profile["tax_id"]
    result["tax_id"] = tax_id
    result["country"] = cc
    return result


def _run_attempt(client: PixHttpClient, token: str, billing: dict[str, str], *, promo_campaign_id: str,
                 require_zero: bool, force_payment_method: bool = False, force_zero_checkout: bool = False,
                 max_poll_attempts: int, country: str = "BR", currency: str = "BRL",
                 oa_confirm_protocol: str = "",
                 on_step: Any, on_qr_update: Any,
                 on_paid: Any = None, wait_paid: bool = False, paid_timeout: float = 900.0,
                 sleep_fn: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    def step(name: str, status: str, detail: str) -> None:
        if callable(on_step):
            on_step({"name": name, "status": status, "detail": str(detail)[:800]})

    country, currency = _checkout_country_currency(country, currency)
    page_url = f"https://chatgpt.com/?promo_campaign={quote(promo_campaign_id, safe='')}"
    # Stripe's BR PIX payment method requires CPF/CNPJ at confirm time. Fail
    # before creating a checkout session when the caller omitted it, rather
    # than sending a malformed request and obscuring the real cause.  Only BR
    # billing requires a tax id; other countries skip this check.
    if country == "BR" and not str(billing.get("tax_id") or "").strip():
        raise RuntimeError("pix_billing_tax_id_required: billing_details[tax_id]")
    try:
        client.attestation = _fetch_attestation(client, page_url)
        page_html = ""
        if client.session is not None:
            try:
                response = client.session.get(page_url, headers={"user-agent": client.ua, "accept": "text/html,*/*", "referer": page_url}, timeout=25)
                page_html = str(response.text or "")
            except Exception:
                pass
        # 客户端版本头自动升级：页面/app 资源常带 prod-<40hex> 部署版本串，
        # 比代码内置常量新鲜时跟随，避免版本陈旧被上游判机器人。
        found_version = re.search(r"prod-[0-9a-f]{40}", page_html) if page_html else None
        if found_version and str(found_version.group(0)) != client.client_version:
            previous = client.client_version
            client.client_version = str(found_version.group(0))
            step("warmup", "ok", f"client version {previous[:18]}… → {client.client_version[:18]}…")
        step("warmup", "ok", f"campaign={promo_campaign_id}; attestation={bool(client.attestation)}")
    except Exception as exc:
        step("warmup", "warn", f"{type(exc).__name__}: non-blocking")

    # The browser sends a fresh checkout-session sentinel with the same
    # device/session/proxy tuple.  Keep generation best-effort so a missing
    # local bridge does not hide the actual upstream response.
    checkout_risk = _sentinel_headers(client, client.base_proxy, "chatgpt_checkout", page_url)
    checkout_headers = _target_headers(_chat_headers(client, token), "/backend-api/payments/checkout", page_url)
    checkout_headers.update(checkout_risk)
    checkout_body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "promo_campaign": {"promo_campaign_id": promo_campaign_id, "is_coupon_from_query_param": False},
        "checkout_ui_mode": "custom",
    }
    status, text, _ = client.request("POST", "https://chatgpt.com/backend-api/payments/checkout", headers=checkout_headers, json_body=checkout_body, proxy=client.base_proxy)
    checkout = _json_response(status, text, "PIX Checkout create")
    cs_id = _first(checkout, ("checkout_session_id", "session_id", "id"))
    if not cs_id:
        raise RuntimeError("PIX checkout session id missing")
    processor = _first(checkout, ("processor_entity", "processorEntity")) or "openai_llc"
    stripe_pk = _first(checkout, ("publishable_key", "stripe_publishable_key"))
    checkout_secret = _first(checkout, ("client_secret", "checkout_session_client_secret"))
    if not stripe_pk:
        raise RuntimeError("PIX Checkout payment prerequisites missing: publishable_key")
    cuss_secret = _first(checkout, ("customer_session_client_secret", "customerSessionClientSecret"))
    manual_approval = bool(checkout.get("requires_manual_approval")) if isinstance(checkout, dict) else False
    referer = f"https://chatgpt.com/checkout/{processor}/{cs_id}"
    step("checkout", "ok",
         f"{currency} custom session={cs_id}; processor={processor}"
         + ("; account requires manual approval" if manual_approval else ""))

    # ── 确认协议选择 ──
    # auto（默认，真实运行）：先走 bax.har 的 confirm-token 新链路；只有请求
    # 契约级错误（未知参数/端点下线/5xx）才回退 legacy payment_pages 链路。
    # 注入 transport 的测试 fixture 是 legacy 端点集，auto 在其下解析为 legacy，
    # 保持既有单元测试语义不变。显式 token/legacy 强制单一路径。
    requested = str(oa_confirm_protocol or _OA_CONFIRM_DEFAULT or "auto").strip().lower()
    if requested not in {"auto", "token", "legacy"}:
        requested = "auto"
    # 注入 transport 的旧 fixture 是 legacy 端点集：仅当调用方没有显式指定协议时
    # 才把 auto 解析为 legacy（保持既有单元测试语义）；显式 auto 走真实 token 优先。
    if requested == "auto" and client.transport is not None and not str(oa_confirm_protocol or "").strip():
        requested = "legacy"
    phases = ["token", "legacy"] if requested == "auto" else [requested]

    payment: dict[str, Any] | None = None
    elements: dict[str, Any] = {}
    amount = 0
    used_protocol = ""
    last_phase_error: BaseException | None = None
    for index, protocol in enumerate(phases):
        used_protocol = protocol
        try:
            if protocol == "token":
                step("protocol", "run", "OA confirm-token 链路（elements→ctoken→checkout/confirm）")
                payment, elements, amount = _confirm_phase_token(
                    client, token, billing, cs_id=cs_id, processor=processor, stripe_pk=stripe_pk,
                    checkout_payload=checkout, cuss_secret=cuss_secret, manual_approval=manual_approval,
                    referer=referer, require_zero=require_zero, force_payment_method=force_payment_method,
                    force_zero_checkout=force_zero_checkout, max_poll_attempts=max_poll_attempts,
                    country=country, currency=currency, on_step=on_step, on_qr_update=on_qr_update,
                    sleep_fn=sleep_fn,
                )
            else:
                step("protocol", "run", "legacy payment_pages 链路（init→confirm→approve）")
                payment, elements, amount = _confirm_phase_legacy(
                    client, token, billing, cs_id=cs_id, processor=processor, stripe_pk=stripe_pk,
                    checkout_secret=checkout_secret, checkout_payload=checkout, referer=referer,
                    require_zero=require_zero, force_payment_method=force_payment_method,
                    force_zero_checkout=force_zero_checkout, max_poll_attempts=max_poll_attempts,
                    country=country, currency=currency, on_step=on_step, on_qr_update=on_qr_update,
                    sleep_fn=sleep_fn,
                )
            break
        except _ProtocolContractError as exc:
            last_phase_error = exc
            payment = None
            if index + 1 < len(phases):
                step("protocol", "warn",
                     f"{protocol} 链路契约错误（{str(exc)[:180]}），回退 {phases[index + 1]} 链路")
                continue
            raise
    if payment is None:
        if last_phase_error is not None:
            raise last_phase_error
        raise RuntimeError("PIX confirm phase produced no payment result")

    qr = _extract_pix_action(payment)
    link = _extract_link(payment)
    state = str((payment.get("submission_attempt") or {}).get("state") or payment.get("status") or "").lower()
    if not link and state in {"succeeded", "complete", "approved", "confirmed"}:
        link = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={processor}&plan_type=plus"
    if not link and qr:
        link = str(qr.get("hosted_instructions_url") or qr.get("qr_url") or "")
    if not link:
        raise RuntimeError(f"PIX payment page did not generate a link: status={state or 'unknown'}; {_diagnostic(payment)}")
    # ── 严格 0 元校验（用户要求：确认二维码是零元链接，非零元一律不输出）──
    # _amount() 优先取 amount_total/amount_subtotal，优惠场景下仍是原价（如 9990），
    # 会误判非 0 元；这里以 Stripe 的权威应付金额（payment_intent.amount /
    # invoice.amount_due / total_summary.due）为准，并叠加二维码 tag54 双重校验。
    payable = _payable_amount(payment)
    qr_amount = _pix_qr_amount(str((qr or {}).get("qr_data") or ""))
    step("zero-check", "ok",
         f"payable={payable} qr_tag54={qr_amount} require_zero={require_zero}")
    if require_zero and not force_zero_checkout:
        if payable is not None and payable != 0:
            raise RuntimeError(f"PIX promo_not_applied: final payable={payable} (stripe)")
        if qr_amount is not None and qr_amount != 0:
            raise RuntimeError(f"PIX promo_not_applied: QR tag54 amount={qr_amount}")
    zero_verified = (payable == 0) or (payable is None and qr_amount in (None, 0))
    paid = state in {"succeeded", "complete", "approved", "confirmed"}
    paid_confirmed_via = ""
    watch_thread = None
    if wait_paid and not paid:
        # GCash-aligned keep-alive: same client session, a background (non-daemon)
        # thread keeps polling Stripe submission state + subscriptions so the
        # process stays alive after the QR/link is handed off and emits
        # [paid]/payment_status events when the payment actually lands.
        try:
            watch_thread = _start_paid_watch(
                client, token, cs_id=cs_id, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id,
                elements=elements, proxy=client.base_proxy, timeout_seconds=paid_timeout,
                on_step=on_step, on_paid=on_paid, sleep_fn=sleep_fn,
            )
            paid_confirmed_via = "watching"
        except Exception as exc:
            step("paid-watch", "warn", f"{type(exc).__name__}: {str(exc)[:200]}")
    result = {
        "ok": True, "link": link, "provider": "pix", "provider_redirect_url": link,
        "checkout_session_id": cs_id, "cs_id": cs_id, "processor_entity": processor,
        "billing_country": country, "currency": currency.upper(), "checkout_currency": currency.upper(),
        "amount": amount, "checkout_amount": amount, "promo_applied": amount == 0,
        "payment_method_types": ["pix"], "elements_session_id": str(elements.get("session_id") or ""),
        "payment_status": state, "paid": paid, "paid_confirmed_via": paid_confirmed_via,
        "qr_url": str((qr or {}).get("qr_url") or ""), "qr_image_url": str((qr or {}).get("image_url_png") or (qr or {}).get("image_url_svg") or ""),
        "qr_data": str((qr or {}).get("qr_data") or ""), "expires_at": int((qr or {}).get("expires_at") or 0),
        # 严格 0 元校验结果：payable 为 Stripe 权威应付金额（minor units），
        # zero_verified=True 表示确认二维码为 0 元链接。
        "zero_verified": bool(zero_verified), "payable_amount": payable, "qr_tag54_amount": qr_amount,
        "confirm_protocol": used_protocol,
    }
    if watch_thread is not None:
        # Signal the CLI that a keep-alive watch is active and the process must
        # stay alive; the thread owns the client session and closes it on exit.
        result["_watch_started"] = True
    return result

def extract_pix_link(access_token: str, proxy: str | None = None, *, billing: dict[str, Any] | None = None,
                     promo_campaign_id: str = PIX_PROMO, require_zero: bool = True,
                     force_payment_method: bool = False, force_zero_checkout: bool = False,
                     max_poll_attempts: int = MAX_POLL_ATTEMPTS,
                     country: str = "BR", currency: str = "BRL",
                     proxy_pool: list[str] | None = None,
                     on_step: Any = None, on_qr_update: Any = None, transport: Transport | None = None,
                     sleep_fn: Callable[[float], None] = time.sleep, fingerprint: dict[str, Any] | None = None,
                     on_paid: Any = None, wait_paid: bool = False, paid_timeout: float = 900.0,
                     oa_confirm_protocol: str = "") -> dict[str, Any]:
    token = normalize_access_token(access_token)
    if not token:
        raise ValueError("Access Token is required")
    proxy_url = normalize_proxy(str(proxy or DEFAULT_PROXY))
    if not proxy_url and transport is None:
        raise RuntimeError("proxy is required for PIX extraction")
    # 用户可自选国家币种；未指定时默认 BR/BRL。
    checkout_country, checkout_currency = _checkout_country_currency(country, currency)
    resolved_billing = _normalize_billing(billing, _jwt_email(token), country=checkout_country)

    # 不在此处做代理池轮换：与其他通道一致，失败后整条返回给 Node 调度层，
    # 由 Node 按「换代理 + 换指纹」统一重试（MAX_AUTO_RETRIES 次）。此处只用主代理跑一次。
    candidates: list[str] = [proxy_url]

    last_error: dict[str, Any] | None = None
    for attempt_index, active_proxy in enumerate(candidates):
        client = PixHttpClient(active_proxy, transport=transport, fingerprint=fingerprint)
        watch_started = False
        try:
            try:
                result = _run_attempt(client, token, resolved_billing, promo_campaign_id=promo_campaign_id,
                                      require_zero=require_zero, force_payment_method=force_payment_method,
                                      force_zero_checkout=force_zero_checkout, max_poll_attempts=max_poll_attempts,
                                      country=checkout_country, currency=checkout_currency,
                                      oa_confirm_protocol=oa_confirm_protocol,
                                      on_step=on_step, on_qr_update=on_qr_update, on_paid=on_paid,
                                      wait_paid=wait_paid, paid_timeout=paid_timeout, sleep_fn=sleep_fn)
                result["steps"] = []
                watch_started = bool(result.pop("_watch_started", False))
                # Surface the exact billing identity used so the caller can audit
                # the generated CPF/CNPJ and address without guessing.
                result["billing"] = {key: resolved_billing[key] for key in ("name", "email", "line1", "city", "state", "postal_code", "country", "tax_id")}
                result["tax_id"] = resolved_billing["tax_id"]
                if len(candidates) > 1:
                    result["proxy_rotated"] = attempt_index > 0
                    result["proxy_index"] = attempt_index
                return result
            except Exception as exc:
                reason, retryable = classify_error(exc)
                message = localize_pix_error(exc)
                last_error = {
                    "ok": False, "link": "", "provider": "pix", "error": message,
                    "reason_code": reason, "retryable": retryable,
                    "extra": {"error_stage": reason.replace("pix_", ""), "retryable": retryable, "billing_country": checkout_country, "currency": checkout_currency, "amount_minor": None},
                }
                # 仅在网络类可重试错误且还有剩余代理时换池重跑；账号/风控/优惠类
                # 错误是终态，不轮换（换代理也无法改变账号资质或优惠资格）。
                if retryable and attempt_index + 1 < len(candidates):
                    if callable(on_step):
                        on_step({"name": "proxy-rotate", "status": "run", "detail": f"主代理失败（{reason}），换池内第 {attempt_index + 2} 条代理重试"})
                    continue
                return last_error
        finally:
            # A keep-alive watch thread owns the client and closes it on exit;
            # closing here would kill the payment watch mid-flight.
            if not watch_started:
                client.close()
    return last_error or {
        "ok": False, "link": "", "provider": "pix", "error": "PIX 提链失败",
        "reason_code": "pix_extract_failed", "retryable": True,
        "extra": {"error_stage": "extract", "retryable": True, "billing_country": checkout_country, "currency": checkout_currency, "amount_minor": None},
    }


def demo_result() -> dict[str, Any]:
    """Deterministic offline result used by CLI smoke tests."""
    payload = {"next_action": {"pix_display_qr_code": {"hosted_instructions_url": "https://payments.stripe.com/pix/demo", "image_url_png": "https://files.stripe.com/pix/demo.png", "data": "000201demo"}}}
    qr = _extract_pix_action(payload) or {}
    return {"ok": True, "link": qr["hosted_instructions_url"], "provider": "pix", "amount": 0, "checkout_amount": 0, "promo_applied": True, "qr_url": qr["qr_url"], "qr_image_url": qr["image_url_png"], "qr_data": qr["qr_data"], "expires_at": 0, "payment_method_types": ["pix"]}


__all__ = [
    "PixHttpClient", "classify_error", "demo_result", "extract_pix_link",
    "localize_pix_error", "normalize_access_token", "normalize_proxy", "_extract_pix_action",
]