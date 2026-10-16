from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests

_CACHE_PATH = Path(os.getenv("PAY153_BILLING_ADDRESS_CACHE", "/opt/pay153/data/billing_addresses.json"))
_CACHE_LOCK = threading.RLock()
_RESOLVE_LOCK = threading.Lock()
_NOMINATIM_LOCK = threading.Lock()
_LAST_NOMINATIM_AT = 0.0
_LAST_PICK: dict[str, str] = {}

# 代理坐标反查缓存。默认按引擎根目录下的 data/ 存放：worker 以引擎目录为工作目录
# 启动子进程，这个相对路径可写；参考包的 /opt/pay153/data 在本项目的部署里没有
# 对应目录，照抄会让缓存写入静默失败，每次提链都要重新请求 Nominatim。
_REVERSE_CACHE_PATH = Path(
    os.getenv(
        "PAY153_NOMINATIM_REVERSE_CACHE",
        str(Path(__file__).resolve().parent / "data" / "nominatim_reverse_cache.json"),
    )
)
_REVERSE_CACHE_LOCK = threading.RLock()
# 坐标 -> 地址在 30 天内视为稳定；Nominatim 返回的地点不会频繁变化。
_REVERSE_CACHE_TTL_SECONDS = 2592000
# 缓存条目上限，超出后按写入时间淘汰最旧的。
_REVERSE_CACHE_MAX_ENTRIES = 5000
_REVERSE_REQUEST_TIMEOUT_SECONDS = 20.0
# Nominatim 公共实例要求全应用最多每秒 1 次请求，这里留出余量。
_NOMINATIM_MIN_INTERVAL_SECONDS = 1.05
_NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_USER_AGENT = "tilian-upi-billing-resolver/1.0"
_RECENT_PICKS: dict[str, list[str]] = {}
_PICK_LOCK = threading.Lock()
_MEMORY_CACHE: dict[str, list[dict[str, str]]] | None = None
_MEMORY_CACHE_MTIME_NS = -1

_COUNTRY_NAMES = {
    "BA": "Bosnia and Herzegovina", "US": "United States", "BR": "Brazil",
    "IN": "India", "DE": "Germany", "NL": "Netherlands", "GB": "United Kingdom",
    "FR": "France", "AU": "Australia", "JP": "Japan", "PH": "Philippines",
}

_BUILTIN_PUBLIC_ADDRESSES: dict[str, list[dict[str, str]]] = {
    "BA": [
        {"name": "Movenpick Sarajevo", "line1": "Fra Filipa Lastrica 3", "city": "Sarajevo", "state": "", "postal_code": "71000", "country": "BA", "source": "builtin_public"},
        {"name": "Hotel Old Sarajevo", "line1": "Bravadziluk 38", "city": "Sarajevo", "state": "", "postal_code": "71000", "country": "BA", "source": "builtin_public"},
        {"name": "Courtyard Sarajevo", "line1": "Skenderija 1", "city": "Sarajevo", "state": "", "postal_code": "71000", "country": "BA", "source": "builtin_public"},
        {"name": "Hotel Europe", "line1": "Vladislava Skarica 5", "city": "Sarajevo", "state": "", "postal_code": "71000", "country": "BA", "source": "builtin_public"},
        {"name": "Hotel Bosna", "line1": "Kralja Petra I Karadjordjevica 97", "city": "Banja Luka", "state": "", "postal_code": "78000", "country": "BA", "source": "builtin_public"},
        {"name": "Hotel Mellain", "line1": "Aleja Alije Izetbegovica 3", "city": "Tuzla", "state": "", "postal_code": "75000", "country": "BA", "source": "builtin_public"},
        {"name": "Residence Inn Sarajevo", "line1": "Skenderija 43", "city": "Sarajevo", "state": "", "postal_code": "71000", "country": "BA", "source": "builtin_public"},
    ],
    "US": [
        {"name": "The Plaza Hotel", "line1": "768 5th Ave", "city": "New York", "state": "NY", "postal_code": "10019", "country": "US", "source": "builtin_public"},
        {"name": "The New Yorker Hotel", "line1": "481 8th Ave", "city": "New York", "state": "NY", "postal_code": "10001", "country": "US", "source": "builtin_public"},
        {"name": "The Beverly Hills Hotel", "line1": "9641 Sunset Blvd", "city": "Beverly Hills", "state": "CA", "postal_code": "90210", "country": "US", "source": "builtin_public"},
        {"name": "Millennium Biltmore Los Angeles", "line1": "506 S Grand Ave", "city": "Los Angeles", "state": "CA", "postal_code": "90071", "country": "US", "source": "builtin_public"},
        {"name": "Palmer House", "line1": "17 E Monroe St", "city": "Chicago", "state": "IL", "postal_code": "60603", "country": "US", "source": "builtin_public"},
        {"name": "InterContinental Miami", "line1": "100 Chopin Plaza", "city": "Miami", "state": "FL", "postal_code": "33131", "country": "US", "source": "builtin_public"},
        {"name": "Omni Dallas Hotel", "line1": "555 S Lamar St", "city": "Dallas", "state": "TX", "postal_code": "75202", "country": "US", "source": "builtin_public"},
        {"name": "Palace Hotel", "line1": "2 New Montgomery St", "city": "San Francisco", "state": "CA", "postal_code": "94105", "country": "US", "source": "builtin_public"},
        {"name": "Fairmont Olympic Hotel", "line1": "411 University St", "city": "Seattle", "state": "WA", "postal_code": "98101", "country": "US", "source": "builtin_public"},
        {"name": "Willard InterContinental", "line1": "1401 Pennsylvania Ave NW", "city": "Washington", "state": "DC", "postal_code": "20004", "country": "US", "source": "builtin_public"},
    ],
    "GB": [
        {"name": "The Savoy", "line1": "Strand", "city": "London", "state": "", "postal_code": "WC2R 0EZ", "country": "GB", "source": "builtin_public"},
        {"name": "The Ritz London", "line1": "150 Piccadilly", "city": "London", "state": "", "postal_code": "W1J 9BR", "country": "GB", "source": "builtin_public"},
        {"name": "The Midland Hotel", "line1": "16 Peter St", "city": "Manchester", "state": "", "postal_code": "M60 2DS", "country": "GB", "source": "builtin_public"},
        {"name": "The Edwardian Manchester", "line1": "Free Trade Hall, Peter St", "city": "Manchester", "state": "", "postal_code": "M2 5GP", "country": "GB", "source": "builtin_public"},
        {"name": "The Grand Hotel Birmingham", "line1": "1 Church St", "city": "Birmingham", "state": "", "postal_code": "B3 2FE", "country": "GB", "source": "builtin_public"},
        {"name": "The Balmoral", "line1": "1 Princes St", "city": "Edinburgh", "state": "", "postal_code": "EH2 2EQ", "country": "GB", "source": "builtin_public"},
        {"name": "Kimpton Blythswood Square", "line1": "11 Blythswood Square", "city": "Glasgow", "state": "", "postal_code": "G2 4AD", "country": "GB", "source": "builtin_public"},
        {"name": "Titanic Hotel Liverpool", "line1": "Stanley Dock, Regent Rd", "city": "Liverpool", "state": "", "postal_code": "L3 0AN", "country": "GB", "source": "builtin_public"},
    ],
    "IN": [
        {"name": "Taj Mahal Palace", "line1": "Apollo Bunder, Colaba", "city": "Mumbai", "state": "MH", "postal_code": "400001", "country": "IN", "source": "builtin_public"},
        {"name": "The Oberoi Mumbai", "line1": "Nariman Point", "city": "Mumbai", "state": "MH", "postal_code": "400021", "country": "IN", "source": "builtin_public"},
        {"name": "The Leela Palace New Delhi", "line1": "Diplomatic Enclave, Chanakyapuri", "city": "New Delhi", "state": "DL", "postal_code": "110023", "country": "IN", "source": "builtin_public"},
        {"name": "ITC Maurya", "line1": "Sardar Patel Marg, Diplomatic Enclave", "city": "New Delhi", "state": "DL", "postal_code": "110021", "country": "IN", "source": "builtin_public"},
        {"name": "The Imperial New Delhi", "line1": "Janpath", "city": "New Delhi", "state": "DL", "postal_code": "110001", "country": "IN", "source": "builtin_public"},
        {"name": "The Lalit New Delhi", "line1": "Barakhamba Avenue", "city": "New Delhi", "state": "DL", "postal_code": "110001", "country": "IN", "source": "builtin_public"},
        {"name": "ITC Gardenia", "line1": "1 Residency Road", "city": "Bengaluru", "state": "KA", "postal_code": "560025", "country": "IN", "source": "builtin_public"},
        {"name": "The Oberoi Bengaluru", "line1": "37-39 MG Road", "city": "Bengaluru", "state": "KA", "postal_code": "560001", "country": "IN", "source": "builtin_public"},
        {"name": "The Leela Palace Bengaluru", "line1": "23 Old Airport Road", "city": "Bengaluru", "state": "KA", "postal_code": "560008", "country": "IN", "source": "builtin_public"},
        {"name": "Sheraton Grand Bengaluru", "line1": "26/1 Dr Rajkumar Road", "city": "Bengaluru", "state": "KA", "postal_code": "560055", "country": "IN", "source": "builtin_public"},
        {"name": "Taj Coromandel", "line1": "37 Mahatma Gandhi Road, Nungambakkam", "city": "Chennai", "state": "TN", "postal_code": "600034", "country": "IN", "source": "builtin_public"},
        {"name": "Hyatt Regency Chennai", "line1": "365 Anna Salai, Teynampet", "city": "Chennai", "state": "TN", "postal_code": "600018", "country": "IN", "source": "builtin_public"},
        {"name": "The Westin Chennai", "line1": "154 Velachery Main Road", "city": "Chennai", "state": "TN", "postal_code": "600042", "country": "IN", "source": "builtin_public"},
        {"name": "The Park Kolkata", "line1": "17 Park Street", "city": "Kolkata", "state": "WB", "postal_code": "700016", "country": "IN", "source": "builtin_public"},
        {"name": "Taj Bengal", "line1": "34B Belvedere Road, Alipore", "city": "Kolkata", "state": "WB", "postal_code": "700027", "country": "IN", "source": "builtin_public"},
        {"name": "Taj Falaknuma Palace", "line1": "Engine Bowli, Falaknuma", "city": "Hyderabad", "state": "TS", "postal_code": "500053", "country": "IN", "source": "builtin_public"},
        {"name": "Taj Krishna", "line1": "Road No. 1, Banjara Hills", "city": "Hyderabad", "state": "TS", "postal_code": "500034", "country": "IN", "source": "builtin_public"},
        {"name": "Trident Hyderabad", "line1": "1-10-147, Begumpet", "city": "Hyderabad", "state": "TS", "postal_code": "500016", "country": "IN", "source": "builtin_public"},
        {"name": "Rambagh Palace", "line1": "Bhawani Singh Road", "city": "Jaipur", "state": "RJ", "postal_code": "302005", "country": "IN", "source": "builtin_public"},
        {"name": "Fairmont Jaipur", "line1": "2, Riico Kukas", "city": "Jaipur", "state": "RJ", "postal_code": "302028", "country": "IN", "source": "builtin_public"},
        {"name": "JW Marriott Pune", "line1": "Senapati Bapat Road", "city": "Pune", "state": "MH", "postal_code": "411053", "country": "IN", "source": "builtin_public"},
        {"name": "Conrad Pune", "line1": "7/7 Mangaldas Road", "city": "Pune", "state": "MH", "postal_code": "411001", "country": "IN", "source": "builtin_public"},
        {"name": "Courtyard by Marriott Ahmedabad", "line1": "Ramdevnagar Road", "city": "Ahmedabad", "state": "GJ", "postal_code": "380054", "country": "IN", "source": "builtin_public"},
        {"name": "Crowne Plaza Ahmedabad City Centre", "line1": "SG Highway", "city": "Ahmedabad", "state": "GJ", "postal_code": "380054", "country": "IN", "source": "builtin_public"},
        {"name": "Hyatt Regency Chandigarh", "line1": "178, Industrial Area Phase 1", "city": "Chandigarh", "state": "CH", "postal_code": "160002", "country": "IN", "source": "builtin_public"},
        {"name": "Clarks Avadh", "line1": "8 Mahatma Gandhi Marg, Hazratganj", "city": "Lucknow", "state": "UP", "postal_code": "226001", "country": "IN", "source": "builtin_public"},
        {"name": "Courtyard by Marriott Bhopal", "line1": "DB City Mall, Arera Hills", "city": "Bhopal", "state": "MP", "postal_code": "462011", "country": "IN", "source": "builtin_public"},
        {"name": "Hotel Maurya Patna", "line1": "South Gandhi Maidan", "city": "Patna", "state": "BR", "postal_code": "800001", "country": "IN", "source": "builtin_public"},
        {"name": "Radisson Blu Ranchi", "line1": "1, Airport Road, Hinoo", "city": "Ranchi", "state": "JH", "postal_code": "834002", "country": "IN", "source": "builtin_public"},
        {"name": "The Crown Bhubaneswar", "line1": "Toshali Plaza, Sahid Nagar", "city": "Bhubaneswar", "state": "OD", "postal_code": "751007", "country": "IN", "source": "builtin_public"},
        {"name": "Radisson Blu Guwahati", "line1": "National Highway 37, Gotanagar", "city": "Guwahati", "state": "AS", "postal_code": "781033", "country": "IN", "source": "builtin_public"},
        {"name": "Taj Malabar Resort & Spa", "line1": "Marine Drive", "city": "Kochi", "state": "KL", "postal_code": "682031", "country": "IN", "source": "builtin_public"},
        {"name": "The Gateway Hotel Beach Road", "line1": "Beach Road", "city": "Visakhapatnam", "state": "AP", "postal_code": "530023", "country": "IN", "source": "builtin_public"},
        {"name": "Vivanta Thiruvananthapuram", "line1": "C V Raman Pillai Road, Kowdiar", "city": "Thiruvananthapuram", "state": "KL", "postal_code": "695003", "country": "IN", "source": "builtin_public"},
        {"name": "The Gateway Hotel Coimbatore", "line1": "66, Avanashi Road", "city": "Coimbatore", "state": "TN", "postal_code": "641037", "country": "IN", "source": "builtin_public"},
    ],
    "PH": [
        {"name": "The Manila Hotel", "line1": "One Rizal Park, Ermita", "city": "Manila", "state": "Metro Manila", "postal_code": "0913", "country": "PH", "source": "builtin_public"},
        {"name": "New World Makati Hotel", "line1": "Esperanza Street corner Makati Avenue", "city": "Makati", "state": "Metro Manila", "postal_code": "1228", "country": "PH", "source": "builtin_public"},
        {"name": "Shangri-La The Fort", "line1": "30th Street corner 5th Avenue, Bonifacio Global City", "city": "Taguig", "state": "Metro Manila", "postal_code": "1634", "country": "PH", "source": "builtin_public"},
        {"name": "Conrad Manila", "line1": "Seaside Boulevard, Mall of Asia Complex", "city": "Pasay", "state": "Metro Manila", "postal_code": "1300", "country": "PH", "source": "builtin_public"},
        {"name": "Okada Manila", "line1": "New Seaside Drive, Entertainment City", "city": "Paranaque", "state": "Metro Manila", "postal_code": "1701", "country": "PH", "source": "builtin_public"},
        {"name": "Seda BGC", "line1": "30th Street corner 11th Avenue, Bonifacio Global City", "city": "Taguig", "state": "Metro Manila", "postal_code": "1634", "country": "PH", "source": "builtin_public"},
    ]
}


def _load_cache() -> dict[str, list[dict[str, str]]]:
    global _MEMORY_CACHE, _MEMORY_CACHE_MTIME_NS
    try:
        mtime_ns = _CACHE_PATH.stat().st_mtime_ns
        if _MEMORY_CACHE is not None and _MEMORY_CACHE_MTIME_NS == mtime_ns:
            return _MEMORY_CACHE
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        _MEMORY_CACHE = data if isinstance(data, dict) else {}
        _MEMORY_CACHE_MTIME_NS = mtime_ns
        return _MEMORY_CACHE
    except Exception:
        return {}


def _save_cache(cache: dict[str, list[dict[str, str]]]) -> None:
    global _MEMORY_CACHE, _MEMORY_CACHE_MTIME_NS
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
        _MEMORY_CACHE = cache
        _MEMORY_CACHE_MTIME_NS = _CACHE_PATH.stat().st_mtime_ns
    except Exception:
        pass


def _reverse_cache_file() -> Path:
    """反查缓存路径。统一转成 Path：调用方可能用字符串覆盖这个模块变量，
    而 str 没有 .parent/.read_text，直接使用会静默丢失缓存。"""
    return Path(_REVERSE_CACHE_PATH)


def _load_reverse_cache() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_reverse_cache_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_reverse_cache(cache: dict[str, dict[str, Any]]) -> None:
    try:
        path = _reverse_cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _reverse_cache_key(lat: float, lon: float, country: str) -> str:
    return f"{lat:.5f},{lon:.5f}|{country}"


def _nominatim_reverse_once(lat: float, lon: float) -> dict[str, Any] | None:
    """按全局限速发一次反向查询，失败返回 None 而不抛错。"""
    global _LAST_NOMINATIM_AT
    with _NOMINATIM_LOCK:
        delay = _NOMINATIM_MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_NOMINATIM_AT)
        if delay > 0:
            time.sleep(delay)
        try:
            response = requests.get(
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


def _reverse_address_from_payload(
    payload: dict[str, Any], lat: float, lon: float, country: str
) -> dict[str, str] | None:
    """把 Nominatim 响应整理成账单字段；城市或街道缺失时返回 None。

    只做字段提取，不判断这是否是付款人的真实账单地址，也不要求邮编存在。
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
    place_name = str(
        address.get("amenity")
        or address.get("building")
        or address.get("shop")
        or address.get("tourism")
        or payload.get("name")
        or ""
    ).strip()
    line1 = " ".join(part for part in (house_number, road) if part).strip()
    if not line1 and place_name:
        line1 = place_name
    elif (
        not house_number
        and place_name
        and road
        and place_name.casefold() != road.casefold()
    ):
        line1 = f"{place_name}, {road}"
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
        "name": place_name,
        "line1": line1,
        "city": city,
        "state": str(address.get("state") or address.get("state_district") or "").strip(),
        "postal_code": str(address.get("postcode") or "").strip(),
        "country": item_country or country,
        "source": "openstreetmap_nominatim_reverse",
        "display_name": str(payload.get("display_name") or "").strip(),
        "latitude": f"{lat:.7f}",
        "longitude": f"{lon:.7f}",
    }


def resolve_reverse_address(
    latitude: Any, longitude: Any, expected_country: str = ""
) -> dict[str, str] | None:
    """按代理出口坐标反查一条账单地址，命中缓存时不联网。

    返回 None 表示坐标无效、反查失败或结果不在 expected_country 内，调用方应回退
    到本地地址池。这里返回的是地图上该坐标附近的地点，不代表付款人的真实账单地址。
    """
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    country = str(expected_country or "").strip().upper()
    cache_key = _reverse_cache_key(lat, lon, country)
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
        payload = _nominatim_reverse_once(lat, lon)
        if payload is not None:
            resolved = _reverse_address_from_payload(payload, lat, lon, country)
            break
    if resolved is None:
        return None

    with _REVERSE_CACHE_LOCK:
        cache = _load_reverse_cache()
        cache[cache_key] = {**resolved, "_cached_at": now}
        if len(cache) > _REVERSE_CACHE_MAX_ENTRIES:
            cache = dict(
                sorted(
                    cache.items(),
                    key=lambda item: float((item[1] or {}).get("_cached_at") or 0),
                    reverse=True,
                )[:_REVERSE_CACHE_MAX_ENTRIES]
            )
        _save_reverse_cache(cache)
    return resolved


def _reset_reverse_cache() -> None:
    """清空坐标反查缓存，供测试与运维手动重置使用。"""
    with _REVERSE_CACHE_LOCK:
        try:
            _reverse_cache_file().unlink()
        except OSError:
            pass


def _component(components: list[dict[str, Any]], kind: str, *, short: bool = False) -> str:
    for item in components or []:
        if kind in (item.get("types") or []):
            return str(item.get("shortText" if short else "longText") or item.get("longText") or "").strip()
    return ""


def _google_places(country: str, city: str, region: str) -> list[dict[str, str]]:
    key = str(os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()
    if not key:
        return []
    country_name = _COUNTRY_NAMES.get(country, country)
    query_city = city or region or country_name
    payload = {
        "textQuery": f"hotels and public businesses in {query_city}, {country_name}",
        "languageCode": "en",
        "maxResultCount": 20,
    }
    resp = requests.post(
        "https://places.googleapis.com/v1/places:searchText",
        json=payload,
        headers={
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.addressComponents",
            "Content-Type": "application/json",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        return []
    out: list[dict[str, str]] = []
    for place in (resp.json() or {}).get("places") or []:
        components = place.get("addressComponents") or []
        item_country = _component(components, "country", short=True).upper()
        street_number = _component(components, "street_number")
        route = _component(components, "route")
        line1 = " ".join(part for part in (route, street_number) if part).strip()
        item_city = (
            _component(components, "locality")
            or _component(components, "postal_town")
            or _component(components, "administrative_area_level_2")
        )
        state = _component(components, "administrative_area_level_1", short=True)
        postal = _component(components, "postal_code")
        if item_country == country and line1 and item_city and postal:
            out.append({
                "name": str((place.get("displayName") or {}).get("text") or "").strip(),
                "line1": line1, "city": item_city, "state": state,
                "postal_code": postal, "country": country, "source": "google_places",
            })
    return out


def _nominatim(country: str, city: str, region: str) -> list[dict[str, str]]:
    global _LAST_NOMINATIM_AT
    country_name = _COUNTRY_NAMES.get(country, country)
    query_city = city or region or country_name
    queries = [
        f"hotel {query_city} {country_name}",
        f"restaurant {query_city} {country_name}",
        f"shopping centre {query_city} {country_name}",
    ]
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for query in queries:
        with _NOMINATIM_LOCK:
            delay = 1.05 - (time.monotonic() - _LAST_NOMINATIM_AT)
            if delay > 0:
                time.sleep(delay)
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query, "format": "jsonv2", "addressdetails": 1,
                    "limit": 12, "countrycodes": country.lower(),
                },
                headers={"User-Agent": "pay153-public-address-resolver/1.0 (main.153.ink)"},
                timeout=20,
            )
            _LAST_NOMINATIM_AT = time.monotonic()
        if resp.status_code != 200:
            continue
        for row in resp.json() or []:
            address = row.get("address") or {}
            item_country = str(address.get("country_code") or "").upper()
            road = str(address.get("road") or address.get("pedestrian") or "").strip()
            house = str(address.get("house_number") or "").strip()
            place_name = str(
                address.get("tourism") or address.get("amenity") or address.get("shop")
                or row.get("name") or ""
            ).strip()
            line1 = " ".join(part for part in (road, house) if part).strip()
            if not house and place_name and road:
                line1 = f"{place_name}, {road}"
            item_city = str(
                address.get("city") or address.get("town") or address.get("village")
                or address.get("municipality") or address.get("county") or ""
            ).strip()
            state = str(address.get("state") or address.get("state_district") or "").strip()
            iso_state = str(address.get("ISO3166-2-lvl4") or address.get("ISO3166-2-lvl6") or "").upper()
            if country == "US" and iso_state.startswith("US-"):
                state = iso_state.split("-", 1)[1]
            postal = str(address.get("postcode") or "").strip()
            signature = (line1.lower(), item_city.lower(), postal.lower())
            if item_country == country and line1 and item_city and postal and signature not in seen:
                seen.add(signature)
                out.append({
                    "name": place_name, "line1": line1, "city": item_city,
                    "state": state, "postal_code": postal, "country": country,
                    "source": "openstreetmap_nominatim",
                })
        if len(out) >= 8:
            break
    return out


def _pick(key: str, rows: list[dict[str, str]], preferred_city: str = "") -> dict[str, str] | None:
    valid = [row for row in rows if row.get("line1") and row.get("city") and row.get("postal_code")]
    wanted = preferred_city.strip().casefold()
    if wanted:
        local = [row for row in valid if wanted in str(row.get("city") or "").casefold() or str(row.get("city") or "").casefold() in wanted]
        if local:
            valid = local
    if not valid:
        return None
    with _PICK_LOCK:
        recent = _RECENT_PICKS.setdefault(key, [])
        recent_set = set(recent)
        choices = [
            row for row in valid
            if f"{row.get('line1')}|{row.get('postal_code')}" not in recent_set
        ] or valid
        selected = dict(random.SystemRandom().choice(choices))
        signature = f"{selected.get('line1')}|{selected.get('postal_code')}"
        _LAST_PICK[key] = signature
        recent.append(signature)
        del recent[:-64]
    return selected


def resolve_cached_country_address(country: str, preferred_city: str = "") -> dict[str, str] | None:
    """Pick an already validated address from the whole country cache.

    This path never performs a network lookup, so high-concurrency checkout
    jobs can rotate addresses without serializing on Nominatim.
    """
    country = str(country or "").upper()
    with _CACHE_LOCK:
        cache = _load_cache()
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for key, values in cache.items():
            if not str(key).upper().startswith(country + "|") or not isinstance(values, list):
                continue
            for row in values:
                if not isinstance(row, dict):
                    continue
                signature = (
                    str(row.get("line1") or "").strip().casefold(),
                    str(row.get("city") or "").strip().casefold(),
                    str(row.get("postal_code") or "").strip().casefold(),
                )
                if not all(signature) or signature in seen:
                    continue
                seen.add(signature)
                rows.append(dict(row))
    return _pick(f"{country}|__country_pool__", rows, preferred_city)


def resolve_public_address(country: str, city: str = "", region: str = "", postal_hint: str = "") -> dict[str, str] | None:
    country = str(country or "").upper()
    city = str(city or "").strip()
    region = str(region or "").strip()
    if country == "IN":
        # UPI 印度账单专用：只从内置真实地址池随机（state 恒为两字母邦码）。
        # 不读缓存、不写缓存、不联网——历史缓存/在线解析混入的州名全称或
        # 低质量地址会破坏 Stripe 账单校验，且在线解析会拖慢提链并受限流。
        return _pick("IN|__builtin__", list(_BUILTIN_PUBLIC_ADDRESSES.get("IN") or []), city)
    cache_key = f"{country}|{city.lower()}|{region.lower()}"
    with _CACHE_LOCK:
        cache = _load_cache()
        cached = list(cache.get(cache_key) or [])
    # Refresh small pools; otherwise randomly reuse the validated cache.
    if len(cached) < 5:
        with _RESOLVE_LOCK:
            with _CACHE_LOCK:
                cache = _load_cache()
                cached = list(cache.get(cache_key) or [])
            if len(cached) < 5:
                builtin = list(_BUILTIN_PUBLIC_ADDRESSES.get(country) or [])
                if len(builtin) >= 5:
                    # 内置真实地址池已足够随机（且 state 为两字母邦码，Stripe 可直接
                    # 校验），不再走 Google/Nominatim 在线解析，避免印度地址被
                    # Nominatim 的低质量结果（州名全称/通用商户名）污染账单。
                    fetched = builtin
                else:
                    fetched = _google_places(country, city, region)
                    if not fetched:
                        try:
                            fetched = _nominatim(country, city, region)
                        except Exception:
                            fetched = []
                merged: list[dict[str, str]] = []
                signatures: set[tuple[str, str, str]] = set()
                if country == "IN":
                    # UPI 印度账单只使用内置真实地址池（state 恒为两字母邦码）：
                    # 历史缓存或在线解析混入的州名全称/低质量地址会破坏 Stripe
                    # 账单校验，这里一律不合并。
                    rows_to_merge = builtin
                else:
                    rows_to_merge = fetched + cached + list(_BUILTIN_PUBLIC_ADDRESSES.get(country) or [])
                for row in rows_to_merge:
                    signature = (str(row.get("line1") or "").lower(), str(row.get("city") or "").lower(), str(row.get("postal_code") or "").lower())
                    if all(signature) and signature not in signatures:
                        signatures.add(signature)
                        merged.append(row)
                cached = merged[:40]
                with _CACHE_LOCK:
                    cache[cache_key] = cached
                    _save_cache(cache)
    if not cached:
        cached = list(_BUILTIN_PUBLIC_ADDRESSES.get(country) or [])
    return _pick(cache_key, cached, city)
