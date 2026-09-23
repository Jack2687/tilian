import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet

from .config import get_settings


def _fernet() -> Fernet:
    settings = get_settings()
    raw = settings.credential_encryption_key.strip().encode()
    if raw:
        try:
            return Fernet(raw)
        except ValueError:
            pass
    derived = hashlib.sha256(settings.owner_hash_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_credential(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def decrypt_credential(value: bytes) -> str:
    return _fernet().decrypt(value).decode()


def encrypt_account_credential(at: str, session_token: str = "") -> bytes:
    if not session_token:
        return encrypt_credential(at)
    return encrypt_credential(json.dumps({"version": 1, "at": at, "sessionToken": session_token}, separators=(",", ":")))


def decrypt_account_credential(value: bytes) -> tuple[str, str]:
    raw = decrypt_credential(value)
    if raw.startswith("{"):
        try:
            account = json.loads(raw)
        except json.JSONDecodeError:
            account = None
        if isinstance(account, dict) and account.get("version") == 1 and isinstance(account.get("at"), str):
            return account["at"], str(account.get("sessionToken") or "")
    return raw, ""


def owner_key(value: str) -> str:
    settings = get_settings()
    return hmac.new(settings.owner_hash_secret.encode(), value.strip().upper().encode(), hashlib.sha256).hexdigest()


def card_hash(value: str) -> str:
    return owner_key(value)


def mask_owner(value: str) -> str:
    raw = value.strip().upper()
    if len(raw) <= 6:
        return "***"
    return f"{raw[:3]}{'*' * min(10, len(raw) - 6)}{raw[-3:]}"


def mask_token(value: str) -> str:
    raw = value.strip()
    if len(raw) <= 20:
        return "***"
    return f"{raw[:12]}...{raw[-6:]}"


def password_hash(value: str) -> str:
    """Hash a user password with a per-password salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, 240_000)
    return "pbkdf2_sha256$240000$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def password_verify(value: str, encoded: str) -> bool:
    try:
        scheme, rounds_text, salt_text, digest_text = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _auth_secret() -> bytes:
    settings = get_settings()
    return (settings.auth_token_secret.strip() or settings.owner_hash_secret).encode()


def issue_user_token(user_id: int, token_version: int = 0) -> str:
    settings = get_settings()
    payload = {"sub": str(user_id), "ver": int(token_version), "exp": int(time.time()) + settings.user_token_ttl_seconds}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(_auth_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_user_token(value: str) -> dict[str, int] | None:
    try:
        body, signature = value.split(".", 1)
        expected = hmac.new(_auth_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return {"user_id": int(payload["sub"]), "token_version": int(payload.get("ver", 0))}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
