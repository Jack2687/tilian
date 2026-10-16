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
import re
import threading
import time
from pathlib import Path
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

# ── 代理坐标反查（与 UPI 账单同源语义）──
# 先探测代理出口的国家/城市/经纬度，再用 Nominatim /reverse 取该坐标附近的
# 真实街道。这里缓存的是坐标 -> 地址，而 _pick_by_city 缓存的是城市名：两者
# 目的不同，前者让街道与出口同点，后者只在没有坐标可用时兜底。
_REVERSE_CACHE_PATH = Path(
    os.getenv(
        "GOPAY_NOMINATIM_REVERSE_CACHE",
        str(Path(__file__).resolve().parent / "data" / "nominatim_reverse_cache.json"),
    )
)
_REVERSE_CACHE_LOCK = threading.RLock()
_REVERSE_CACHE_TTL_SECONDS = 2592000  # 30 天
_REVERSE_CACHE_MAX_ENTRIES = 5000
_REVERSE_REQUEST_TIMEOUT_SECONDS = 20.0
_NOMINATIM_LOCK = threading.Lock()
_LAST_NOMINATIM_AT = 0.0
_NOMINATIM_MIN_INTERVAL_SECONDS = 1.05  # 公共实例限速 1 次/秒，留余量
_NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_USER_AGENT = "tilian-gopay-billing-resolver/1.0"

_GEO_PROBE_URL = (
    "http://ip-api.com/json/"
    "?fields=status,countryCode,regionName,city,zip,lat,lon"
)
_GEO_PROBE_TIMEOUT_SECONDS = 8.0
_GEO_PROBE_IMPERSONATE = "chrome136"


def _reverse_cache_file() -> Path:
    """反查缓存路径。统一转成 Path：调用方可能用字符串覆盖模块变量，
    而 str 没有 .parent/.read_text，直接用会静默丢失缓存。"""
    return Path(_REVERSE_CACHE_PATH)


def _load_reverse_cache() -> dict:
    try:
        data = json.loads(_reverse_cache_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_reverse_cache(cache: dict) -> None:
    try:
        path = _reverse_cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _reset_reverse_cache() -> None:
    """清空坐标反查缓存，供测试与运维手动重置使用。"""
    with _REVERSE_CACHE_LOCK:
        try:
            _reverse_cache_file().unlink()
        except OSError:
            pass


def _nominatim_reverse_once(lat: float, lon: float):
    """按全局限速发一次反向查询，失败返回 None 而不抛错。"""
    global _LAST_NOMINATIM_AT
    try:
        from curl_cffi import requests as cr
    except Exception:
        return None
    with _NOMINATIM_LOCK:
        delay = _NOMINATIM_MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_NOMINATIM_AT)
        if delay > 0:
            time.sleep(delay)
        try:
            response = cr.get(
                _NOMINATIM_REVERSE_URL,
                params={
                    "format": "jsonv2",
                    "lat": f"{lat:.7f}",
                    "lon": f"{lon:.7f}",
                    "addressdetails": 1,
                    "accept-language": "en",
                    "zoom": 18,
                    "layer": "address",
                },
                headers={"User-Agent": _NOMINATIM_USER_AGENT},
                timeout=_REVERSE_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception:
            return None
        finally:
            _LAST_NOMINATIM_AT = time.monotonic()
    if response.status_code != 200:
        return None
    try:
        payload = response.json() or {}
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _reverse_address_from_payload(payload: dict, lat: float, lon: float, country: str):
    """把 Nominatim 响应整理成账单地址字段；街道或城市缺失时返回 None。

    刻意不返回 name：Nominatim 的 name 是商户/地标名，不是人名，不能当账单姓名。
    """
    address = payload.get("address") or {}
    item_country = str(address.get("country_code") or "").strip().upper()
    if country and item_country != country:
        return None
    road = str(
        address.get("road")
        or address.get("pedestrian")
        or address.get("residential")
        or address.get("footway")
        or address.get("path")
        or ""
    ).strip()
    house_number = str(address.get("house_number") or "").strip()
    line1 = " ".join(part for part in (road, house_number) if part).strip()
    if not line1:
        line1 = str(address.get("amenity") or address.get("building") or "").strip()
    city = str(
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("city_district")
        or address.get("county")
        or ""
    ).strip()
    if not line1 or not city:
        return None
    return {
        "line1": line1,
        "line2": "",
        "city": city,
        "state": str(address.get("state") or address.get("state_district") or "").strip(),
        "postal_code": str(address.get("postcode") or "").strip(),
        "country": item_country or country,
        "source": "openstreetmap_nominatim_reverse",
        "latitude": f"{lat:.7f}",
        "longitude": f"{lon:.7f}",
    }


def resolve_reverse_address(latitude, longitude, expected_country: str = ""):
    """按代理出口坐标反查一条账单地址，命中缓存时不联网。

    返回 None 表示坐标无效、反查失败或结果不在 expected_country 内，调用方应回退
    到内置街道表。这里返回的是地图上该坐标附近的地点，不代表付款人的真实账单地址。
    """
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    country = str(expected_country or "").strip().upper()
    cache_key = f"{lat:.5f},{lon:.5f}|{country}"
    now = time.time()
    with _REVERSE_CACHE_LOCK:
        cached = _load_reverse_cache().get(cache_key) or {}
    if (
        isinstance(cached, dict)
        and now - float(cached.get("_cached_at") or 0) <= _REVERSE_CACHE_TTL_SECONDS
    ):
        return {
            str(key): str(value)
            for key, value in cached.items()
            if key != "_cached_at" and value is not None
        }

    resolved = None
    for _ in range(2):
        try:
            payload = _nominatim_reverse_once(lat, lon)
        except Exception:
            payload = None
        if payload is not None:
            try:
                resolved = _reverse_address_from_payload(payload, lat, lon, country)
            except Exception:
                resolved = None
            break
    if resolved is None:
        return None

    with _REVERSE_CACHE_LOCK:
        cache = _load_reverse_cache()
        cache[cache_key] = {**resolved, "_cached_at": now}
        if len(cache) > _REVERSE_CACHE_MAX_ENTRIES:
            cache = dict(sorted(
                cache.items(),
                key=lambda item: float((item[1] or {}).get("_cached_at") or 0),
                reverse=True,
            )[:_REVERSE_CACHE_MAX_ENTRIES])
        _save_reverse_cache(cache)
    return resolved


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


def _reverse_address_fields(reverse_address: dict) -> Optional[dict]:
    """取出反查结果的地址字段。

    `resolve_reverse_address()` 返回扁平结构（line1/city 在顶层），而调用方也可能
    传嵌套的 ``{"address": {...}}``；两种形状都接受，统一成扁平字典。
    """
    if not isinstance(reverse_address, dict):
        return None
    nested = reverse_address.get("address")
    return nested if isinstance(nested, dict) else reverse_address


def _is_valid_reverse_address(address: dict, country: str) -> bool:
    """反查地址是否可用于建单：街道、城市、省、5 位邮编都要齐。"""
    if not isinstance(address, dict):
        return False
    if str(address.get("country") or country).strip().upper() != country:
        return False
    if not str(address.get("line1") or "").strip():
        return False
    if not str(address.get("city") or "").strip():
        return False
    if not str(address.get("state") or "").strip():
        return False
    return bool(re.fullmatch(r"\d{5}", str(address.get("postal_code") or "").strip()))


def _billing_from_reverse(reverse_address: dict, country: str, email: str, rng: random.Random) -> Optional[dict]:
    """用反查地址组账单；地址不可用时返回 None 让调用方回退内置街道表。

    姓名始终由本地姓名库生成，不取地图上的地点名。
    """
    if not isinstance(reverse_address, dict):
        return None
    address = _reverse_address_fields(reverse_address)
    if not _is_valid_reverse_address(address, country):
        return None
    first = rng.choice(_FIRST_NAMES)
    last = rng.choice(_LAST_NAMES)
    name = first + " " + last
    account_email = str(email or "").strip()
    billing_email = account_email if account_email else (
        first.lower() + "." + last.lower() + str(rng.randint(1000, 9999)) + "@gmail.com"
    )
    phone = rng.choice(_ID_PHONE_PREFIX) + " " + str(rng.randint(100, 999)) + " " + str(rng.randint(1000, 9999))
    return {
        "name": name,
        "email": billing_email,
        "phone": phone,
        "tax_id": "",
        "address": {
            "country": country,
            "line1": " ".join(str(address.get("line1") or "").split()),
            "line2": str(address.get("line2") or "").strip(),
            "city": " ".join(str(address.get("city") or "").split()),
            "state": " ".join(str(address.get("state") or "").split()),
            "postal_code": str(address.get("postal_code") or "").strip(),
        },
        "_place_name": "",
        "_geo_city_matched": False,
        "_source": str(reverse_address.get("source") or "openstreetmap_nominatim_reverse"),
    }


def generate_id_billing(
    email: str = "",
    *,
    country: str = "ID",
    currency: str = "IDR",
    seed: Optional[str] = None,
    geo_city: str = "",
    reverse_address: Optional[dict] = None,
) -> dict:
    """生成印尼账单（真实街道级地址）。

    优先级：环境变量注入 > 坐标反查地址 > 内置街道表（可同城匹配）。
    - reverse_address：resolve_reverse_address() 的结果，缺字段时自动回退；
    - geo_city：代理出口城市（可空），同城优先；
    - seed：账号维度 → 同账号每次同一地址；None → 每次随机；
    - MIN_GOPAY_BILLING_ADDRESS 注入时优先使用。
    """
    injected = _load_env_address()
    if injected:
        return injected

    rng = random.Random(seed if seed is not None else None)
    from_reverse = _billing_from_reverse(reverse_address, country, email, rng)
    if from_reverse is not None:
        return from_reverse

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


def probe_exit_geo(proxy: str = "", timeout: float = 5.0) -> dict:
    """探测代理出口的国家、城市与经纬度；失败返回空字典。

    经纬度是账单地址反查的前置输入。探测失败不影响提链，调用方回退内置街道表。
    """
    if not proxy:
        return {}
    try:
        from curl_cffi import requests as cr
        session = cr.Session(impersonate=_GEO_PROBE_IMPERSONATE)
    except Exception:
        return {}
    try:
        try:
            resp = session.get(
                _GEO_PROBE_URL,
                proxies={"http": proxy, "https": proxy},
                timeout=min(max(float(timeout or _GEO_PROBE_TIMEOUT_SECONDS), 2.0), _GEO_PROBE_TIMEOUT_SECONDS),
            )
            data = resp.json() or {}
            if str(data.get("status") or "").lower() == "success":
                return {
                    "country": str(data.get("countryCode") or "").upper().strip(),
                    "region": str(data.get("regionName") or "").strip(),
                    "city": str(data.get("city") or "").strip(),
                    "postal": str(data.get("zip") or "").strip(),
                    "latitude": str(data.get("lat") or "").strip(),
                    "longitude": str(data.get("lon") or "").strip(),
                }
        except Exception:
            pass
        # ip-api 不可达时退回 ipwho.is，只取城市（没有坐标就无从反查）。
        resp = session.get(
            "https://ipwho.is/",
            proxies={"http": proxy, "https": proxy},
            timeout=min(max(float(timeout or _GEO_PROBE_TIMEOUT_SECONDS), 2.0), _GEO_PROBE_TIMEOUT_SECONDS),
        )
        data = resp.json() or {}
        if str(data.get("success", True)).lower() != "false":
            return {
                "country": str(data.get("country_code") or "").upper().strip(),
                "region": str(data.get("region") or "").strip(),
                "city": str(data.get("city") or "").strip(),
                "postal": str(data.get("postal") or "").strip(),
                "latitude": str(data.get("latitude") or "").strip(),
                "longitude": str(data.get("longitude") or "").strip(),
            }
    except Exception:
        return {}
    finally:
        try:
            session.close()
        except Exception:
            pass
    return {}


def probe_exit_city(proxy: str = "", timeout: float = 5.0) -> str:
    """探测代理出口城市，失败返回 ''。

    仅用于让账单地址与代理出口同城（更真实）；探测失败不影响随机生成。
    """
    return str(probe_exit_geo(proxy, timeout=timeout).get("city") or "")


__all__ = ["generate_id_billing", "probe_exit_city", "probe_exit_geo", "resolve_reverse_address"]
