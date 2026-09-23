# -*- coding: utf-8 -*-
"""Polish billing identities for the BLIK checkout flow."""
from __future__ import annotations

import hashlib
import random
import re
import unicodedata


_ADDRESSES = (
    ("ul. Marszałkowska", "Warszawa", "mazowieckie", "00-590", 1, 99),
    ("ul. Nowy Świat", "Warszawa", "mazowieckie", "00-497", 1, 69),
    ("ul. Puławska", "Warszawa", "mazowieckie", "02-515", 1, 99),
    ("Aleje Jerozolimskie", "Warszawa", "mazowieckie", "00-024", 1, 99),
    ("ul. Długa", "Kraków", "małopolskie", "31-146", 1, 60),
    ("ul. Karmelicka", "Kraków", "małopolskie", "31-128", 1, 70),
    ("ul. Starowiślna", "Kraków", "małopolskie", "31-032", 1, 99),
    ("ul. Kalwaryjska", "Kraków", "małopolskie", "30-504", 1, 99),
    ("ul. Piotrkowska", "Łódź", "łódzkie", "90-001", 1, 120),
    ("ul. Narutowicza", "Łódź", "łódzkie", "90-135", 1, 99),
    ("Długi Targ", "Gdańsk", "pomorskie", "80-828", 1, 50),
    ("al. Grunwaldzka", "Gdańsk", "pomorskie", "80-241", 1, 99),
    ("ul. Świętojańska", "Gdynia", "pomorskie", "81-368", 1, 99),
    ("ul. Świdnicka", "Wrocław", "dolnośląskie", "50-066", 1, 70),
    ("ul. Oławska", "Wrocław", "dolnośląskie", "50-123", 1, 60),
    ("ul. Piekary", "Poznań", "wielkopolskie", "61-823", 1, 70),
    ("ul. Święty Marcin", "Poznań", "wielkopolskie", "61-803", 1, 99),
    ("ul. Jagiellońska", "Szczecin", "zachodniopomorskie", "70-435", 1, 99),
    ("al. Wojska Polskiego", "Szczecin", "zachodniopomorskie", "70-470", 1, 99),
    ("ul. 3 Maja", "Katowice", "śląskie", "40-096", 1, 45),
    ("ul. Warszawska", "Katowice", "śląskie", "40-009", 1, 80),
    ("Krakowskie Przedmieście", "Lublin", "lubelskie", "20-002", 1, 80),
    ("ul. Lipowa", "Białystok", "podlaskie", "15-424", 1, 70),
    ("ul. 3 Maja", "Rzeszów", "podkarpackie", "35-030", 1, 70),
    ("ul. Szeroka", "Toruń", "kujawsko-pomorskie", "87-100", 1, 60),
    ("ul. Gdańska", "Bydgoszcz", "kujawsko-pomorskie", "85-005", 1, 99),
    ("ul. Dąbrowszczaków", "Olsztyn", "warmińsko-mazurskie", "10-538", 1, 50),
    ("ul. Sienkiewicza", "Kielce", "świętokrzyskie", "25-354", 1, 99),
    ("ul. Krakowska", "Opole", "opolskie", "45-018", 1, 70),
    ("ul. Kupiecka", "Zielona Góra", "lubuskie", "65-426", 1, 80),
)
_FIRST_NAMES = (
    "Adam", "Jakub", "Piotr", "Michał", "Paweł", "Krzysztof", "Tomasz", "Mateusz",
    "Anna", "Julia", "Katarzyna", "Zofia", "Alicja", "Magdalena", "Aleksandra", "Joanna",
)
_LAST_NAMES = (
    "Nowak", "Kowalski", "Wiśniewski", "Wójcik", "Kowalczyk", "Kamiński",
    "Lewandowski", "Zieliński", "Szymański", "Woźniak", "Dąbrowski", "Kozłowski",
)
_MOBILE_PREFIXES = (
    500, 501, 502, 503, 504, 505, 506, 507, 508, 509, 510, 511, 512, 513,
    514, 515, 516, 517, 518, 519, 530, 531, 532, 533, 534, 535, 536, 537,
    570, 571, 572, 573, 574, 575, 576, 577, 578, 579, 600, 601, 602, 603,
)


def _email_part(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", normalized.lower())


def generate_pl_billing(email: str = "", seed: str = "") -> dict[str, object]:
    material = str(seed or email or random.random()).encode("utf-8")
    rng = random.Random(int.from_bytes(hashlib.sha256(material).digest()[:8], "big"))
    street, city, state, postal_code, house_min, house_max = rng.choice(_ADDRESSES)
    first = rng.choice(_FIRST_NAMES)
    last = rng.choice(_LAST_NAMES)
    billing_email = str(email or "").strip() or (
        f"{_email_part(first)}.{_email_part(last)}{rng.randint(10, 999)}@gmail.com"
    )
    house = rng.randint(house_min, house_max)
    apartment = f"/{rng.randint(1, 80)}" if rng.random() < 0.35 else ""
    prefix = rng.choice(_MOBILE_PREFIXES)
    return {
        "name": f"{first} {last}",
        "email": billing_email,
        "phone": f"+48 {prefix} {rng.randint(100, 999)} {rng.randint(100, 999)}",
        "address": {
            "country": "PL",
            "line1": f"{street} {house}{apartment}",
            "line2": "",
            "city": city,
            "state": state,
            "postal_code": postal_code,
        },
        "_source": "builtin_pl_city_postcode_v2",
    }


__all__ = ["generate_pl_billing"]
