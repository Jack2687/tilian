from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, Field, field_validator


SUPPORTED_MODES = {"paypal", "gopay", "gcash", "momo", "upi", "blik", "pix", "ideal", "card"}


class AccountInput(BaseModel):
    email: str = ""
    at: str = Field(min_length=8)
    sessionToken: str = Field(default="", max_length=32768, validation_alias=AliasChoices("sessionToken", "session_token"))
    channel: str | None = None


class TaskCreate(BaseModel):
    mode: str
    cardCode: str = Field(min_length=3, max_length=160)
    accounts: list[AccountInput] = Field(default_factory=list, max_length=100)
    atsText: str = ""
    concurrency: int = Field(default=3, ge=1, le=100)
    proxyMode: str = "builtin"
    proxiesText: str = ""
    exitProxiesText: str = ""
    paramsText: str = ""

    @field_validator("mode")
    @classmethod
    def allowed_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_MODES:
            raise ValueError("不支持的提链通道")
        return normalized

    @field_validator("proxyMode")
    @classmethod
    def allowed_proxy_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"builtin", "custom"}:
            raise ValueError("不支持的代理模式")
        return normalized


class CardCheck(BaseModel):
    code: str = Field(min_length=3, max_length=160)


class RegisterInput(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_\-\.]+$")
    password: str = Field(min_length=6, max_length=128)
    displayName: str = Field(default="", max_length=120)


class LoginInput(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class AdminUserUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    displayName: str | None = Field(default=None, max_length=120)
    taskConcurrency: int | None = Field(default=None, ge=1, le=100)
    maxAccountsPerTask: int | None = Field(default=None, ge=1, le=1000)
    allowFusion: bool | None = None
    notes: str | None = Field(default=None, max_length=500)


class UserPolicyConfig(BaseModel):
    registrationEnabled: bool = True
    maxUsers: int = Field(default=10000, ge=1, le=10_000_000)
    defaultConcurrency: int = Field(default=3, ge=1, le=100)
    maxAccountsPerTask: int = Field(default=100, ge=1, le=1000)
    allowFusion: bool = True


class CardFuseInput(BaseModel):
    codes: list[str] = Field(min_length=2, max_length=50)
    remark: str = Field(default="CDK 融合", max_length=160)

    @field_validator("codes")
    @classmethod
    def normalize_codes(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().upper() for item in value if item.strip()]
        if len(normalized) < 2:
            raise ValueError("请至少输入 2 个 CDK")
        if len(set(normalized)) != len(normalized):
            raise ValueError("融合列表中存在重复 CDK")
        return normalized


class AccountParse(BaseModel):
    text: str = Field(max_length=2_000_000)


class CardImportItem(BaseModel):
    code: str = Field(min_length=3, max_length=160)
    quota: int = Field(ge=1, le=1_000_000)


class CardImport(BaseModel):
    cards: list[CardImportItem] = Field(min_length=1, max_length=1000)


class CardGenerateInput(BaseModel):
    count: int = Field(default=1, ge=1, le=500)
    quota: int = Field(default=100, ge=1, le=1_000_000)
    days: int = Field(default=30, ge=1, le=3650)
    remark: str = Field(default="", max_length=160)
    fusionDisabled: bool = False


class ConcurrencyConfig(BaseModel):
    default: int = Field(default=3, ge=1, le=100)
    vip: int = Field(default=10, ge=1, le=200)
    globalLimit: int = Field(default=100, ge=1, le=1000)


class ProxyPoolChannel(BaseModel):
    enabled: bool = False
    primaryText: str = Field(default="", max_length=200_000)
    exitText: str = Field(default="", max_length=200_000)


class ProxyPoolsConfig(BaseModel):
    channels: dict[str, ProxyPoolChannel] = Field(default_factory=dict)

    @field_validator("channels")
    @classmethod
    def allowed_channels(cls, value: dict[str, ProxyPoolChannel]) -> dict[str, ProxyPoolChannel]:
        unknown = set(value) - SUPPORTED_MODES
        if unknown:
            raise ValueError(f"不支持的代理池通道: {', '.join(sorted(unknown))}")
        return value


class AnnouncementInput(BaseModel):
    title: str = Field(default="系统公告", max_length=160)
    content: str = Field(min_length=1, max_length=5000)


class AdminCardStatusInput(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|disabled|expired|fused)$")
    fusionDisabled: bool | None = None


class BlikCodeInput(BaseModel):
    itemIndex: int = Field(ge=0, le=100)
    code: str = Field(min_length=6, max_length=6, pattern="^[0-9]{6}$")


class QrRefreshInput(BaseModel):
    itemIndex: int = Field(default=0, ge=0, le=100)


class ErrorResponse(BaseModel):
    code: str
    message: str
    trace_id: str
    details: dict[str, Any] | None = None
