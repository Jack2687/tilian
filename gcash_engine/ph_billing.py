# -*- coding: utf-8 -*-
"""菲律宾账单地址生成器 v2 —— 真实街道级地址（拒付/blk 修复）。

设计原则（解决"地址不够真实"）：
  1. 真实菲律宾街道 + 门牌号 + 所在区(barangay) + 城市 + 4位邮政编码，
     全部来自菲律宾邮政真实体系（全国邮编为 4 位数字，Manila 09xx/10xx 等）；
  2. 姓名菲律宾化（常见姓氏组合），邮箱/手机号（+63）配套；
  3. 支持代理出口城市匹配：调用方传入 geo.city（代理实际出口），地址优先用同城；
  4. 同账号固定：seed=账号维度时，同账号每次提链地址一致（不飘移）；
  5. 支持环境变量 MIN_GCASH_BILLING_ADDRESS（JSON）注入您自己验证过的真实地址，
     注入后优先使用，完全绕开内置库。
"""
from __future__ import annotations

import json
import os
import random
from typing import Optional

# ── 真实菲律宾街道地址库（城市/区/街道/邮编，均为真实邮政数据）──
# 每项: (line1 街道, barangay/区, city, state/大区, postal 4位)
_PH_STREETS = [
    # Manila（马尼拉，首都区）
    ("Jose Rizal Avenue", "Santa Cruz", "Manila", "Metro Manila", "1003"),
    ("Taft Avenue", "Ermita", "Manila", "Metro Manila", "1000"),
    ("Roxas Boulevard", "Ermita", "Manila", "Metro Manila", "1000"),
    ("United Nations Avenue", "Paco", "Manila", "Metro Manila", "1007"),
    ("Quezon Boulevard", "Quiapo", "Manila", "Metro Manila", "1001"),
    ("Rizal Avenue", "Santa Cruz", "Manila", "Metro Manila", "1003"),
    ("Padre Faura Street", "Ermita", "Manila", "Metro Manila", "1000"),
    ("Justice Jose Abad Santos Avenue", "Tondo", "Manila", "Metro Manila", "1012"),
    # Makati（马卡蒂，金融区）
    ("Ayala Avenue", "San Lorenzo", "Makati", "Metro Manila", "1223"),
    ("Makati Avenue", "Poblacion", "Makati", "Metro Manila", "1210"),
    ("Paseo de Roxas", "San Antonio", "Makati", "Metro Manila", "1226"),
    ("Senator Gil Puyat Avenue", "Bel Air", "Makati", "Metro Manila", "1209"),
    ("J.P. Rizal Avenue", "Poblacion", "Makati", "Metro Manila", "1210"),
    # Quezon City（奎松市）
    ("Timog Avenue", "Kamuning", "Quezon City", "Metro Manila", "1103"),
    ("Quezon Avenue", "Pinyahan", "Quezon City", "Metro Manila", "1100"),
    ("Commonwealth Avenue", "Batasan Hills", "Quezon City", "Metro Manila", "1126"),
    ("E. Rodriguez Sr. Avenue", "Bagong Pag-asa", "Quezon City", "Metro Manila", "1105"),
    ("Espana Boulevard", "Sampaloc", "Quezon City", "Metro Manila", "1008"),
    # Taguig（BGC 金融区）
    ("Bonifacio Global City", "Bonifacio Global City", "Taguig", "Metro Manila", "1634"),
    ("McKinley Parkway", "Fort Bonifacio", "Taguig", "Metro Manila", "1630"),
    ("C5 Road", "Ususan", "Taguig", "Metro Manila", "1632"),
    # Pasay / Paranaque / Pasig
    ("EDSA", "Roxas Boulevard", "Pasay", "Metro Manila", "1300"),
    ("Macapagal Boulevard", "Diosdado Macapagal", "Pasay", "Metro Manila", "1300"),
    ("Ninoy Aquino Avenue", "Paranaque", "Paranaque", "Metro Manila", "1700"),
    ("Ortigas Avenue", "San Antonio", "Pasig", "Metro Manila", "1600"),
    # 外省（吕宋主要城市，真实城市/邮编）
    ("Lapulapu Street", "Lapu-Lapu", "Cebu City", "Central Visayas", "6000"),
    ("Osmena Boulevard", "Osmena", "Cebu City", "Central Visayas", "6000"),
    ("Jose Maria Basa Street", "Poblacion", "Iloilo City", "Western Visayas", "5000"),
    ("Rizal Street", "Poblacion", "Davao City", "Davao Region", "8000"),
    ("Claro M. Recto Avenue", "Poblacion", "Davao City", "Davao Region", "8000"),
    ("Session Road", "Poblacion", "Baguio", "Cordillera", "2600"),
    ("Maginhawa Street", "Teachers Village East", "Quezon City", "Metro Manila", "1101"),
    ("Kalayaan Avenue", "Pinyahan", "Quezon City", "Metro Manila", "1100"),
]

# 菲律宾常见姓名（性别中性分布）
_FIRST_NAMES = ["Jose", "Maria", "Juan", "Ana", "Pedro", "Liza", "Ramon", "Cristina", "Miguel", "Rosa",
                "Antonio", "Carmen", "Rafael", "Lourdes", "Emmanuel", "Josefa", "Carlos", "Angelica",
                "Fernando", "Gloria", "Ricardo", "Teresa", "Dominic", "Nerissa", "Eduardo", "Beatrice",
                "Marco", "Alma", "Dante", "Corazon"]
_LAST_NAMES = ["Santos", "Reyes", "Cruz", "Bautista", "Garcia", "Mendoza", "Torres", "Flores",
               "Ramos", "Aquino", "Villanueva", "Domingo", "Salazar", "Navarro", "Perez", "Del Rosario",
               "Castillo", "Gonzales", "Ocampo", "Santiago", "Magbanua", "Tolentino", "Villareal", "Buenaventura"]

_PH_PHONE_PREFIX = ["+63 917", "+63 918", "+63 920", "+63 921", "+63 927", "+63 929", "+63 939", "+63 949"]


def _load_env_address() -> Optional[dict]:
    """环境变量注入：MIN_GCASH_BILLING_ADDRESS 为 JSON（您自己验证过的真实地址）。"""
    raw = str(os.environ.get("MIN_GCASH_BILLING_ADDRESS") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("address"):
        # 兼容平铺字段
        data = {"name": data.get("name", ""), "email": data.get("email", ""),
                "address": data}
    addr = data.get("address") or {}
    if not (str(addr.get("line1") or "") and str(addr.get("city") or "") and str(addr.get("postal_code") or "")):
        return None
    return {
        "name": str(data.get("name") or "Jose Santos"),
        "email": str(data.get("email") or ""),
        "phone": str(data.get("phone") or ""),
        "tax_id": str(data.get("tax_id") or ""),
        "address": {
            "country": str(addr.get("country") or "PH"),
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
    "manila": "Manila", "ermita": "Manila", "santa cruz": "Manila", "tondo": "Manila", "metro manila": "Metro Manila",
    "makati": "Makati", "quezon city": "Quezon City", "qc": "Quezon City",
    "taguig": "Taguig", "bgc": "Taguig", "fort bonifacio": "Taguig", "bonifacio global city": "Taguig",
    "pasay": "Pasay", "paranaque": "Paranaque", "pasig": "Pasig",
    "cebu": "Cebu City", "cebu city": "Cebu City", "iloilo": "Iloilo City",
    "davao": "Davao City", "baguio": "Baguio", "clark": "Angeles", "angeles": "Angeles",
    "mandaue": "Mandaue", "lapu-lapu": "Lapu-Lapu", "lapu lapu": "Lapu-Lapu",
}


def _normalize_city(city: str) -> str:
    key = str(city or "").strip().lower()
    return _CITY_ALIASES.get(key, "")


def _pick_by_city(rng: random.Random, city: str) -> tuple:
    """按代理出口城市挑选匹配街道；匹配不到则全库随机。"""
    norm = _normalize_city(city)
    if norm:
        candidates = [row for row in _PH_STREETS if row[2].lower() in (norm.lower(), norm.lower().replace(" city", ""))]
        if candidates:
            return rng.choice(candidates)
    return rng.choice(_PH_STREETS)


def generate_ph_billing(
    email: str = "",
    *,
    country: str = "PH",
    currency: str = "PHP",
    seed: Optional[str] = None,
    geo_city: str = "",
) -> dict:
    """生成菲律宾账单（真实街道级地址）。

    - geo_city：代理出口城市（可空），同城优先；
    - seed：账号维度 → 同账号每次同一地址；None → 每次随机；
    - MIN_GCASH_BILLING_ADDRESS 注入时优先使用。
    """
    injected = _load_env_address()
    if injected:
        return injected

    rng = random.Random(seed if seed is not None else None)
    street, barangay, city, state, postal = _pick_by_city(rng, geo_city)
    first = rng.choice(_FIRST_NAMES)
    last = rng.choice(_LAST_NAMES)
    name = first + " " + last
    house = rng.randint(20, 2000)
    # 门牌+街道，楼层/单元放 line2（可选），保持自然
    line1 = str(house) + " " + street
    line2 = "Barangay " + barangay if barangay else ""
    phone = rng.choice(_PH_PHONE_PREFIX) + " " + str(rng.randint(100, 999)) + " " + str(rng.randint(1000, 9999))
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
        "_barangay": barangay,
        "_geo_city_matched": bool(_normalize_city(geo_city) and _normalize_city(geo_city).lower() in (city.lower(), city.lower().replace(" city", ""))),
        "_source": "builtin_public_ph_street",
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


__all__ = ["generate_ph_billing", "probe_exit_city"]
