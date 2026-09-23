# -*- coding: utf-8 -*-
"""印尼账单地址生成器 —— 真实街道级地址（GoPay 提链 / 拒付风控）。

设计原则（对齐 gcash_engine/ph_billing.py）：
  1. 真实印尼街道 + 门牌号 + 城市 + 5 位邮政编码，
     均来自印尼邮政真实体系（雅加达 10xxx/12xxx、泗水 60xxx、万隆 40xxx 等）；
  2. 姓名印尼化（常见姓氏组合），邮箱/手机号（+62）配套；
  3. 支持代理出口城市匹配：调用方传入 geo.city（代理实际出口），地址优先用同城；
  4. 同账号固定：seed=账号维度时，同账号每次提链地址一致（不飘移）；
  5. 支持环境变量 MIN_GOPAY_BILLING_ADDRESS（JSON）注入自己验证过的真实地址，
     注入后优先使用，完全绕开内置库。
"""
from __future__ import annotations

import json
import os
import random
from typing import Optional

# ── 真实印尼街道地址库（城市/街道/邮编，均为真实邮政数据）──
# 每项: (line1 街道, city, state/省, postal 5位)
# 覆盖雅加达、泗水、万隆、棉兰、三宝垄、日惹、登巴萨、望加锡、棉兰、巴淡、巨港、
# 北干巴鲁、茂物、德波、唐格朗、勿加泗等主要城市，街道数量扩充以减少批量提链时
# 「姓名+地址」的高频撞车，避免被上游识别为重复/异常账单。
_ID_STREETS = [
    # Jakarta（雅加达，首都特区）
    ("Jl. Jend. Sudirman", "Jakarta", "DKI Jakarta", "12190"),
    ("Jl. M.H. Thamrin", "Jakarta", "DKI Jakarta", "10350"),
    ("Jl. Gatot Subroto", "Jakarta", "DKI Jakarta", "12710"),
    ("Jl. Rasuna Said", "Jakarta", "DKI Jakarta", "12950"),
    ("Jl. Kemang Raya", "Jakarta", "DKI Jakarta", "12730"),
    ("Jl. Senopati", "Jakarta", "DKI Jakarta", "12190"),
    ("Jl. Kebon Sirih", "Jakarta", "DKI Jakarta", "10110"),
    ("Jl. Hayam Wuruk", "Jakarta", "DKI Jakarta", "11160"),
    ("Jl. Fatmawati", "Jakarta", "DKI Jakarta", "12420"),
    ("Jl. Cipete Raya", "Jakarta", "DKI Jakarta", "12410"),
    ("Jl. Tebet Raya", "Jakarta", "DKI Jakarta", "12810"),
    ("Jl. Pramuka", "Jakarta", "DKI Jakarta", "13120"),
    ("Jl. Pemuda", "Jakarta", "DKI Jakarta", "13220"),
    ("Jl. Matraman Raya", "Jakarta", "DKI Jakarta", "13150"),
    ("Jl. Pangeran Jayakarta", "Jakarta", "DKI Jakarta", "10730"),
    ("Jl. Mangga Besar", "Jakarta", "DKI Jakarta", "11180"),
    ("Jl. Pluit Raya", "Jakarta", "DKI Jakarta", "14450"),
    ("Jl. Pantai Indah Kapuk", "Jakarta", "DKI Jakarta", "14470"),
    ("Jl. Kelapa Gading Boulevard", "Jakarta", "DKI Jakarta", "14240"),
    ("Jl. Boulevard Raya", "Jakarta", "DKI Jakarta", "14240"),
    ("Jl. S. Parman", "Jakarta", "DKI Jakarta", "11440"),
    ("Jl. Tomang Raya", "Jakarta", "DKI Jakarta", "11440"),
    ("Jl. Daan Mogot", "Jakarta", "DKI Jakarta", "11510"),
    ("Jl. Letjen S. Parman", "Jakarta", "DKI Jakarta", "11440"),
    # Surabaya（泗水，东爪哇）
    ("Jl. Tunjungan", "Surabaya", "Jawa Timur", "60272"),
    ("Jl. Darmo", "Surabaya", "Jawa Timur", "60241"),
    ("Jl. Basuki Rahmat", "Surabaya", "Jawa Timur", "60271"),
    ("Jl. Mayjen Sungkono", "Surabaya", "Jawa Timur", "60256"),
    ("Jl. Ahmad Yani", "Surabaya", "Jawa Timur", "60271"),
    ("Jl. Raya Gubeng", "Surabaya", "Jawa Timur", "60281"),
    ("Jl. Diponegoro", "Surabaya", "Jawa Timur", "60241"),
    ("Jl. Kertajaya", "Surabaya", "Jawa Timur", "60282"),
    ("Jl. Raya Darmo Permai", "Surabaya", "Jawa Timur", "60226"),
    ("Jl. Kalibokor", "Surabaya", "Jawa Timur", "60272"),
    # Bandung（万隆，西爪哇）
    ("Jl. Braga", "Bandung", "Jawa Barat", "40111"),
    ("Jl. Asia Afrika", "Bandung", "Jawa Barat", "40261"),
    ("Jl. Dago", "Bandung", "Jawa Barat", "40135"),
    ("Jl. Ir. H. Juanda", "Bandung", "Jawa Barat", "40115"),
    ("Jl. Merdeka", "Bandung", "Jawa Barat", "40117"),
    ("Jl. Setiabudi", "Bandung", "Jawa Barat", "40143"),
    ("Jl. Dipati Ukur", "Bandung", "Jawa Barat", "40132"),
    ("Jl. Buah Batu", "Bandung", "Jawa Barat", "40262"),
    ("Jl. Riau", "Bandung", "Jawa Barat", "40111"),
    ("Jl. Cihampelas", "Bandung", "Jawa Barat", "40131"),
    # Medan（棉兰，北苏门答腊）
    ("Jl. Gatot Subroto", "Medan", "Sumatera Utara", "20114"),
    ("Jl. Iskandar Muda", "Medan", "Sumatera Utara", "20154"),
    ("Jl. Sisingamangaraja", "Medan", "Sumatera Utara", "20217"),
    ("Jl. Diponegoro", "Medan", "Sumatera Utara", "20152"),
    ("Jl. Imam Bonjol", "Medan", "Sumatera Utara", "20151"),
    # Semarang（三宝垄，中爪哇）
    ("Jl. Pandanaran", "Semarang", "Jawa Tengah", "50134"),
    ("Jl. Pemuda", "Semarang", "Jawa Tengah", "50132"),
    ("Jl. Gajah Mada", "Semarang", "Jawa Tengah", "50133"),
    ("Jl. Diponegoro", "Semarang", "Jawa Tengah", "50241"),
    ("Jl. Sultan Agung", "Semarang", "Jawa Tengah", "50252"),
    # Yogyakarta（日惹）
    ("Jl. Malioboro", "Yogyakarta", "DI Yogyakarta", "55213"),
    ("Jl. Solo", "Yogyakarta", "DI Yogyakarta", "55224"),
    ("Jl. Kaliurang", "Yogyakarta", "DI Yogyakarta", "55281"),
    ("Jl. Parangtritis", "Yogyakarta", "DI Yogyakarta", "55141"),
    ("Jl. Cik Di Tiro", "Yogyakarta", "DI Yogyakarta", "55223"),
    # Denpasar（登巴萨，巴厘）
    ("Jl. Sudirman", "Denpasar", "Bali", "80232"),
    ("Jl. Hayam Wuruk", "Denpasar", "Bali", "80235"),
    ("Jl. Teuku Umar", "Denpasar", "Bali", "80113"),
    ("Jl. Gatot Subroto", "Denpasar", "Bali", "80237"),
    ("Jl. Imam Bonjol", "Denpasar", "Bali", "80119"),
    # Makassar（望加锡，南苏拉威西）
    ("Jl. Sudirman", "Makassar", "Sulawesi Selatan", "90111"),
    ("Jl. A.P. Pettarani", "Makassar", "Sulawesi Selatan", "90222"),
    ("Jl. Perintis Kemerdekaan", "Makassar", "Sulawesi Selatan", "90245"),
    ("Jl. Boulevard", "Makassar", "Sulawesi Selatan", "90222"),
    # Palembang（巨港，南苏门答腊）
    ("Jl. Sudirman", "Palembang", "Sumatera Selatan", "30128"),
    ("Jl. Basuki Rahmat", "Palembang", "Sumatera Selatan", "30127"),
    ("Jl. Demang Lebar Daun", "Palembang", "Sumatera Selatan", "30151"),
    ("Jl. Radial", "Palembang", "Sumatera Selatan", "30126"),
    # Pekanbaru（北干巴鲁，廖内）
    ("Jl. Sudirman", "Pekanbaru", "Riau", "28116"),
    ("Jl. Jend. Sudirman", "Pekanbaru", "Riau", "28116"),
    ("Jl. Tuanku Tambusai", "Pekanbaru", "Riau", "28117"),
    ("Jl. Nangka", "Pekanbaru", "Riau", "28113"),
    # Batam（巴淡，廖内群岛）
    ("Jl. Sudirman", "Batam", "Kepulauan Riau", "29432"),
    ("Jl. Imam Bonjol", "Batam", "Kepulauan Riau", "29444"),
    ("Jl. Raja Haji", "Batam", "Kepulauan Riau", "29432"),
    # Bogor / Depok / Tangerang / Bekasi（大雅加达环带）
    ("Jl. Pajajaran", "Bogor", "Jawa Barat", "16143"),
    ("Jl. Raya Bogor", "Depok", "Jawa Barat", "16424"),
    ("Jl. Margonda Raya", "Depok", "Jawa Barat", "16424"),
    ("Jl. Raya Serpong", "Tangerang", "Banten", "15310"),
    ("Jl. Daan Mogot", "Tangerang", "Banten", "15111"),
    ("Jl. KH. Hasyim Ashari", "Tangerang", "Banten", "15111"),
    ("Jl. Ahmad Yani", "Bekasi", "Jawa Barat", "17141"),
    ("Jl. Chairil Anwar", "Bekasi", "Jawa Barat", "17143"),
]

# 印尼常见姓名（性别中性分布）
_FIRST_NAMES = ["Andi", "Budi", "Siti", "Dewi", "Agus", "Rina", "Bambang", "Sri",
                "Hendra", "Lina", "Ahmad", "Putri", "Dian", "Eko", "Fitri", "Joko",
                "Rudi", "Maya", "Tono", "Sari", "Yanto", "Ratna", "Hadi", "Wulan",
                "Arif", "Nina", "Dedi", "Lestari", "Gunawan", "Indah", "Rizky",
                "Mega", "Fajar", "Yuni", "Taufik", "Rosa", "Bayu", "Citra",
                "Dodi", "Eka", "Farhan", "Gita", "Intan", "Kiki", "Lukman",
                "Mila", "Nanda", "Oki", "Putra", "Rahmat", "Sinta", "Tika", "Umar",
                "Vina", "Wawan", "Yoga", "Zaki"]
_LAST_NAMES = ["Santoso", "Wijaya", "Pratama", "Saputra", "Susanto", "Setiawan",
               "Kusuma", "Hidayat", "Wibowo", "Utama", "Nugroho", "Firmansyah",
               "Kurniawan", "Suryadi", "Rahmawati", "Handayani", "Prasetyo", "Gunawan",
               "Purnama", "Hartono", "Halim", "Pangestu", "Lestari", "Maulana",
               "Siregar", "Nasution", "Sihombing", "Hutapea", "Simanjuntak", "Panggabean",
               "Tampubolon", "Sitorus", "Manullang", "Panjaitan", "Hutabarat", "Sitompul",
               "Sembiring", "Ginting", "Tarigan", "Purba", "Sinaga", "Damanik",
               "Saragih", "Malau", "Pasaribu", "Napitupulu", "Hutasoit", "Siahaan",
               "Sianipar", "Tambunan", "Lumbanbatu", "Hutagalung", "Sipayung", "Barus"]

_ID_PHONE_PREFIX = ["+62 812", "+62 813", "+62 821", "+62 857", "+62 896", "+62 878", "+62 851", "+62 823"]


def _load_env_address() -> Optional[dict]:
    """环境变量注入：MIN_GOPAY_BILLING_ADDRESS 为 JSON（自己验证过的真实地址）。"""
    raw = str(os.environ.get("MIN_GOPAY_BILLING_ADDRESS") or "").strip()
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
        "name": str(data.get("name") or "Budi Santoso"),
        "email": str(data.get("email") or ""),
        "phone": str(data.get("phone") or ""),
        "tax_id": str(data.get("tax_id") or ""),
        "address": {
            "country": str(addr.get("country") or "ID"),
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
    "jakarta": "Jakarta", "dki jakarta": "Jakarta", "jakarta selatan": "Jakarta",
    "jakarta pusat": "Jakarta", "jakarta barat": "Jakarta", "jakarta timur": "Jakarta",
    "jakarta utara": "Jakarta",
    "surabaya": "Surabaya", "bandung": "Bandung", "medan": "Medan",
    "yogyakarta": "Yogyakarta", "semarang": "Semarang", "denpasar": "Denpasar",
    "makassar": "Makassar", "tangerang": "Tangerang", "bogor": "Bogor",
    "depok": "Depok", "bekasi": "Bekasi", "south jakarta": "Jakarta",
    "central jakarta": "Jakarta",
    "palembang": "Palembang", "pekanbaru": "Pekanbaru", "batam": "Batam",
    "kepulauan riau": "Batam",
}


def _normalize_city(city: str) -> str:
    key = str(city or "").strip().lower()
    return _CITY_ALIASES.get(key, "")


def _pick_by_city(rng: random.Random, city: str) -> tuple:
    """按代理出口城市挑选匹配街道；匹配不到则全库随机。"""
    norm = _normalize_city(city)
    if norm:
        candidates = [row for row in _ID_STREETS if row[1].lower() == norm.lower()]
        if candidates:
            return rng.choice(candidates)
    return rng.choice(_ID_STREETS)


def generate_id_billing(
    email: str = "",
    *,
    country: str = "ID",
    currency: str = "IDR",
    seed: Optional[str] = None,
    geo_city: str = "",
) -> dict:
    """生成印尼账单（真实街道级地址）。

    - geo_city：代理出口城市（可空），同城优先；
    - seed：账号维度 → 同账号每次同一地址；None → 每次随机；
    - MIN_GOPAY_BILLING_ADDRESS 注入时优先使用。
    """
    injected = _load_env_address()
    if injected:
        return injected

    rng = random.Random(seed if seed is not None else None)
    street, city, state, postal = _pick_by_city(rng, geo_city)
    first = rng.choice(_FIRST_NAMES)
    last = rng.choice(_LAST_NAMES)
    name = first + " " + last
    house = rng.randint(1, 180)
    # 门牌+街道，楼层/单元放 line2（可选），保持自然
    line1 = "Jl. " + street.replace("Jl. ", "") + " No. " + str(house)
    line2 = ""
    phone = rng.choice(_ID_PHONE_PREFIX) + " " + str(rng.randint(100, 999)) + " " + str(rng.randint(1000, 9999))
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
        "_source": "builtin_public_id_street",
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


__all__ = ["generate_id_billing", "probe_exit_city"]
