# -*- coding: utf-8 -*-
"""越南账单地址生成器 —— 真实街道级地址（MoMo 提链 / 拒付风控）。

设计原则（对齐 gopay_engine/id_billing.py、gcash_engine/ph_billing.py）：
  1. 真实越南街道 + 门牌号 + 城市/郡 + 邮编，覆盖河内、胡志明市、岘港、
     海防、芹苴、芽庄、顺化、头顿等主要城市；
  2. 姓名越南化（常见姓+名组合），邮箱/手机号（+84）配套；
  3. 支持代理出口城市匹配：调用方传入 geo_city（代理实际出口），地址优先同城；
  4. 同账号固定：seed=账号维度时，同账号每次提链地址一致（不飘移）；
  5. 支持环境变量 MIN_MOMO_BILLING_ADDRESS（JSON）注入自己验证过的真实地址，
     注入后优先使用，完全绕开内置库。
"""
from __future__ import annotations

import json
import os
import random
from typing import Optional

# ── 真实越南街道地址库（城市/街道/邮编，均为真实邮政数据）──
# 每项: (line1 街道, city, state/省, postal)
_VN_STREETS = [
    # Hà Nội（河内，首都）
    ("Tràng Tiền", "Hanoi", "Hà Nội", "100000"),
    ("Bà Triệu", "Hanoi", "Hà Nội", "100000"),
    ("Hai Bà Trưng", "Hanoi", "Hà Nội", "100000"),
    ("Lý Thường Kiệt", "Hanoi", "Hà Nội", "100000"),
    ("Trần Hưng Đạo", "Hanoi", "Hà Nội", "100000"),
    ("Phan Chu Trinh", "Hanoi", "Hà Nội", "100000"),
    ("Lê Duẩn", "Hanoi", "Hà Nội", "100000"),
    ("Nguyễn Thái Học", "Hanoi", "Hà Nội", "100000"),
    ("Điện Biên Phủ", "Hanoi", "Hà Nội", "100000"),
    ("Kim Mã", "Hanoi", "Hà Nội", "100000"),
    ("Ngọc Khánh", "Hanoi", "Hà Nội", "100000"),
    ("Đội Cấn", "Hanoi", "Hà Nội", "100000"),
    ("Thụy Khuê", "Hanoi", "Hà Nội", "100000"),
    ("Phan Đình Phùng", "Hanoi", "Hà Nội", "100000"),
    ("Quán Thánh", "Hanoi", "Hà Nội", "100000"),
    ("Hoàng Diệu", "Hanoi", "Hà Nội", "100000"),
    ("Tràng Thi", "Hanoi", "Hà Nội", "100000"),
    ("Đinh Tiên Hoàng", "Hanoi", "Hà Nội", "100000"),
    ("Hàng Bài", "Hanoi", "Hà Nội", "100000"),
    ("Nguyễn Du", "Hanoi", "Hà Nội", "100000"),
    ("Phố Huế", "Hanoi", "Hà Nội", "100000"),
    ("Lò Đúc", "Hanoi", "Hà Nội", "100000"),
    ("Minh Khai", "Hanoi", "Hà Nội", "100000"),
    ("Bạch Mai", "Hanoi", "Hà Nội", "100000"),
    ("Đại Cồ Việt", "Hanoi", "Hà Nội", "100000"),
    ("Tôn Đức Thắng", "Hanoi", "Hà Nội", "100000"),
    ("Nguyễn Lương Bằng", "Hanoi", "Hà Nội", "100000"),
    ("Tây Sơn", "Hanoi", "Hà Nội", "100000"),
    ("Láng", "Hanoi", "Hà Nội", "100000"),
    ("Cầu Giấy", "Hanoi", "Hà Nội", "100000"),
    ("Xuân Thủy", "Hanoi", "Hà Nội", "100000"),
    ("Phạm Hùng", "Hanoi", "Hà Nội", "100000"),
    ("Trần Duy Hưng", "Hanoi", "Hà Nội", "100000"),
    # Hồ Chí Minh City（胡志明市）
    ("Nguyễn Huệ", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Lê Lợi", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Đồng Khởi", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Hai Bà Trưng", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Lý Tự Trọng", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Trần Hưng Đạo", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Cách Mạng Tháng Tám", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Võ Văn Tần", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Điện Biên Phủ", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Nguyễn Đình Chiểu", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Lê Văn Sỹ", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Phan Xích Long", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Nguyễn Thị Minh Khai", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Đinh Tiên Hoàng", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Tôn Đức Thắng", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Nguyễn Tất Thành", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Trường Chinh", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Cộng Hòa", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Nguyễn Văn Linh", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    ("Hoàng Diệu 2", "Ho Chi Minh City", "Hồ Chí Minh", "700000"),
    # Đà Nẵng（岘港）
    ("Bạch Đằng", "Da Nang", "Đà Nẵng", "550000"),
    ("Trần Phú", "Da Nang", "Đà Nẵng", "550000"),
    ("Lê Duẩn", "Da Nang", "Đà Nẵng", "550000"),
    ("Nguyễn Văn Linh", "Da Nang", "Đà Nẵng", "550000"),
    ("Đống Đa", "Da Nang", "Đà Nẵng", "550000"),
    ("Hoàng Diệu", "Da Nang", "Đà Nẵng", "550000"),
    ("Hùng Vương", "Da Nang", "Đà Nẵng", "550000"),
    ("Điện Biên Phủ", "Da Nang", "Đà Nẵng", "550000"),
    # Hải Phòng（海防）
    ("Điện Biên Phủ", "Hai Phong", "Hải Phòng", "180000"),
    ("Trần Phú", "Hai Phong", "Hải Phòng", "180000"),
    ("Lạch Tray", "Hai Phong", "Hải Phòng", "180000"),
    ("Lê Hồng Phong", "Hai Phong", "Hải Phòng", "180000"),
    ("Hoàng Văn Thụ", "Hai Phong", "Hải Phòng", "180000"),
    ("Trần Hưng Đạo", "Hai Phong", "Hải Phòng", "180000"),
    # Cần Thơ（芹苴）
    ("Hòa Bình", "Can Tho", "Cần Thơ", "900000"),
    ("Trần Văn Khéo", "Can Tho", "Cần Thơ", "900000"),
    ("Nguyễn Trãi", "Can Tho", "Cần Thơ", "900000"),
    ("30 Tháng 4", "Can Tho", "Cần Thơ", "900000"),
    ("Lý Tự Trọng", "Can Tho", "Cần Thơ", "900000"),
    # Nha Trang（芽庄，庆和省）
    ("Trần Phú", "Nha Trang", "Khánh Hòa", "650000"),
    ("Nguyễn Thị Minh Khai", "Nha Trang", "Khánh Hòa", "650000"),
    ("Lê Thánh Tôn", "Nha Trang", "Khánh Hòa", "650000"),
    ("Thống Nhất", "Nha Trang", "Khánh Hòa", "650000"),
    ("Hùng Vương", "Nha Trang", "Khánh Hòa", "650000"),
    # Huế（顺化，承天顺化省）
    ("Lê Lợi", "Hue", "Thừa Thiên Huế", "530000"),
    ("Trần Hưng Đạo", "Hue", "Thừa Thiên Huế", "530000"),
    ("Hùng Vương", "Hue", "Thừa Thiên Huế", "530000"),
    ("Nguyễn Huệ", "Hue", "Thừa Thiên Huế", "530000"),
    # Vũng Tàu（头顿，巴地头顿省）
    ("Hạ Long", "Vung Tau", "Bà Rịa - Vũng Tàu", "790000"),
    ("Trần Hưng Đạo", "Vung Tau", "Bà Rịa - Vũng Tàu", "790000"),
    ("Lý Thường Kiệt", "Vung Tau", "Bà Rịa - Vũng Tàu", "790000"),
    ("Nguyễn An Ninh", "Vung Tau", "Bà Rịa - Vũng Tàu", "790000"),
    # Hội An（会安，广南省）
    ("Trần Phú", "Hoi An", "Quảng Nam", "560000"),
    ("Nguyễn Thái Học", "Hoi An", "Quảng Nam", "560000"),
    # Đà Lạt（大叻，林同省）
    ("Trần Phú", "Da Lat", "Lâm Đồng", "670000"),
    ("Nguyễn Văn Cừ", "Da Lat", "Lâm Đồng", "670000"),
    ("Phan Đình Phùng", "Da Lat", "Lâm Đồng", "670000"),
]

# 越南常见姓名（姓 + 中名 + 名，性别中性分布）
_FIRST_NAMES = ["Minh", "Anh", "Hùng", "Lan", "Hương", "Đức", "Thảo", "Quang",
                "Hoa", "Linh", "Tuấn", "Trang", "Nam", "Mai", "Phương", "Khánh",
                "Dũng", "Nhung", "Sơn", "Yến", "Tùng", "Huyền", "Long", "Châu",
                "Việt", "Dung", "Hoàng", "Nga", "Thắng", "Bích", "Hà", "Tâm",
                "Vũ", "Phúc", "Hiếu", "Ngọc", "Tiến", "Thu", "Kiên", "Thủy",
                "Bảo", "Hằng", "Cường", "My", "Giang", "Trúc", "Phong", "Diệp",
                "Duy", "Uyên"]
_LAST_NAMES = ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Huỳnh", "Phan", "Vũ",
               "Võ", "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô", "Dương", "Lý",
               "Đinh", "Trịnh", "Đoàn", "Mai"]

# 越南手机号前缀（+84，越南手机为 9 位号码，常见 9/3 开头段）
_VN_PHONE_PREFIX = ["+84 90", "+84 91", "+84 93", "+84 96", "+84 97", "+84 98",
                    "+84 32", "+84 33", "+84 34", "+84 35", "+84 36", "+84 37", "+84 38", "+84 39"]


def _load_env_address() -> Optional[dict]:
    """环境变量注入：MIN_MOMO_BILLING_ADDRESS 为 JSON（自己验证过的真实地址）。"""
    raw = str(os.environ.get("MIN_MOMO_BILLING_ADDRESS") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("address"):
        data = {"name": data.get("name", ""), "email": data.get("email", ""),
                "address": data}
    addr = data.get("address") or {}
    if not (str(addr.get("line1") or "") and str(addr.get("city") or "") and str(addr.get("postal_code") or "")):
        return None
    return {
        "name": str(data.get("name") or "Minh Nguyen"),
        "email": str(data.get("email") or ""),
        "phone": str(data.get("phone") or ""),
        "tax_id": str(data.get("tax_id") or ""),
        "address": {
            "country": str(addr.get("country") or "VN"),
            "line1": str(addr.get("line1") or ""),
            "line2": str(addr.get("line2") or ""),
            "city": str(addr.get("city") or ""),
            "state": str(addr.get("state") or ""),
            "postal_code": str(addr.get("postal_code") or ""),
        },
        "_place_name": str(data.get("place") or data.get("_place_name") or "user_injected"),
        "_source": "env_injected",
    }


_CITY_ALIASES = {
    "hanoi": "Hanoi", "hà nội": "Hanoi", "ha noi": "Hanoi", "ha nội": "Hanoi",
    "ho chi minh": "Ho Chi Minh City", "ho chi minh city": "Ho Chi Minh City",
    "hồ chí minh": "Ho Chi Minh City", "saigon": "Ho Chi Minh City", "sài gòn": "Ho Chi Minh City",
    "da nang": "Da Nang", "đà nẵng": "Da Nang",
    "hai phong": "Hai Phong", "hải phòng": "Hai Phong",
    "can tho": "Can Tho", "cần thơ": "Can Tho",
    "nha trang": "Nha Trang", "hue": "Hue", "huế": "Hue",
    "vung tau": "Vung Tau", "vũng tàu": "Vung Tau",
    "hoi an": "Hoi An", "hội an": "Hoi An",
    "da lat": "Da Lat", "đà lạt": "Da Lat",
}


def _normalize_city(city: str) -> str:
    key = str(city or "").strip().lower()
    return _CITY_ALIASES.get(key, "")


def _pick_by_city(rng: random.Random, city: str) -> tuple:
    """按代理出口城市挑选匹配街道；匹配不到则全库随机。"""
    norm = _normalize_city(city)
    if norm:
        candidates = [row for row in _VN_STREETS if row[1].lower() == norm.lower()]
        if candidates:
            return rng.choice(candidates)
    return rng.choice(_VN_STREETS)


def generate_vn_billing(
    email: str = "",
    *,
    country: str = "VN",
    currency: str = "VND",
    seed: Optional[str] = None,
    geo_city: str = "",
) -> dict:
    """生成越南账单（真实街道级地址）。

    - geo_city：代理出口城市（可空），同城优先；
    - seed：账号维度 → 同账号每次同一地址；None → 每次随机；
    - MIN_MOMO_BILLING_ADDRESS 注入时优先使用。
    """
    injected = _load_env_address()
    if injected:
        return injected

    rng = random.Random(seed if seed is not None else None)
    street, city, state, postal = _pick_by_city(rng, geo_city)
    last = rng.choice(_LAST_NAMES)
    middle = rng.choice(_FIRST_NAMES)
    first = rng.choice(_FIRST_NAMES)
    name = last + " " + middle + " " + first
    house = rng.randint(1, 380)
    line1 = str(house) + " " + street
    line2 = ""
    phone = rng.choice(_VN_PHONE_PREFIX) + " " + str(rng.randint(1000, 9999)) + " " + str(rng.randint(100, 999))
    account_email = str(email or "").strip()
    billing_email = account_email if account_email else (first.lower() + "." + last.lower() + str(rng.randint(1000, 9999)) + "@gmail.com")
    return {
        "name": name,
        "email": billing_email,
        "phone": phone,
        "tax_id": "",
        "address": {
            "country": country,
            "line1": line1,
            "line2": line2,
            "city": city,
            "state": state,
            "postal_code": postal,
        },
        "_place_name": street,
        "_geo_city_matched": bool(_normalize_city(geo_city) and _normalize_city(geo_city).lower() == city.lower()),
        "_source": "builtin_public_vn_street",
    }


def probe_exit_city(proxy: str = "", timeout: float = 5.0) -> str:
    """探测代理出口城市（ipwho.is 优先，ip-api 兜底），失败返回 ''。

    仅用于让账单地址与代理出口同城（更真实）；探测失败不影响随机生成。
    """
    if not proxy:
        return ""
    try:
        from curl_cffi import requests as cr
        session = cr.Session(impersonate="chrome136")
    except Exception:
        return ""
    try:
        resp = session.get("https://ipwho.is/", proxies={"http": proxy, "https": proxy} if proxy else {}, timeout=timeout)
    except Exception:
        return ""
    try:
        data = resp.json() or {}
        if str(data.get("success", True)).lower() != "false" and data.get("city"):
            return str(data["city"])
    except Exception:
        pass
    try:
        resp = session.get("http://ip-api.com/json/", proxies={"http": proxy, "https": proxy} if proxy else {}, timeout=timeout)
        data = resp.json() or {}
        if str(data.get("status") or "") == "success" and data.get("city"):
            return str(data["city"])
    except Exception:
        pass
    return ""


__all__ = ["generate_vn_billing", "probe_exit_city"]
