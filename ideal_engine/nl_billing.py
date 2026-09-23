# -*- coding: utf-8 -*-
"""Random Dutch billing details for the iDEAL checkout flow.

The iDEAL provider owns this generator so adding address rotation here cannot
change the billing behavior of the other providers.  The address rows are
public streets/business locations spread across Dutch cities.  A call without
``seed`` uses ``SystemRandom`` and therefore produces a fresh identity for
each checkout; ``seed`` is available only for deterministic tests.
"""
from __future__ import annotations

import random
import re
from typing import Optional


# (street, city, province, postal code, minimum house number, maximum house number)
# Postal codes are kept in their Dutch four-digit + two-letter representation.
_NL_STREETS = [
    ("Damrak", "Amsterdam", "Noord-Holland", "1012 LG", 1, 120),
    ("Keizersgracht", "Amsterdam", "Noord-Holland", "1016 GB", 250, 450),
    ("Prinsengracht", "Amsterdam", "Noord-Holland", "1016 GZ", 200, 500),
    ("Amstel", "Amsterdam", "Noord-Holland", "1017 AE", 1, 180),
    ("Stadhouderskade", "Amsterdam", "Noord-Holland", "1072 AB", 1, 160),
    ("Overtoom", "Amsterdam", "Noord-Holland", "1054 HT", 1, 300),
    ("Coolsingel", "Rotterdam", "Zuid-Holland", "3012 AA", 1, 120),
    ("Weena", "Rotterdam", "Zuid-Holland", "3012 CM", 1, 100),
    ("Wilhelminakade", "Rotterdam", "Zuid-Holland", "3072 AP", 1, 200),
    ("Westblaak", "Rotterdam", "Zuid-Holland", "3012 KM", 1, 180),
    ("Kruiskade", "Rotterdam", "Zuid-Holland", "3012 EH", 1, 150),
    ("Spui", "Den Haag", "Zuid-Holland", "2511 BT", 1, 100),
    ("Lange Voorhout", "Den Haag", "Zuid-Holland", "2514 EG", 1, 100),
    ("Johan de Wittlaan", "Den Haag", "Zuid-Holland", "2517 JR", 1, 100),
    ("Carnegieplein", "Den Haag", "Zuid-Holland", "2517 KJ", 1, 80),
    ("Jaarbeursplein", "Utrecht", "Utrecht", "3521 AR", 1, 30),
    ("Oudegracht", "Utrecht", "Utrecht", "3511 AL", 1, 300),
    ("Neude", "Utrecht", "Utrecht", "3512 AE", 1, 50),
    ("Stationsplein", "Utrecht", "Utrecht", "3511 ED", 1, 100),
    ("Vestdijk", "Eindhoven", "Noord-Brabant", "5611 CA", 1, 100),
    ("Markt", "Eindhoven", "Noord-Brabant", "5611 EB", 1, 60),
    ("Stationsweg", "Eindhoven", "Noord-Brabant", "5611 AA", 1, 100),
    ("Emmasingel", "Eindhoven", "Noord-Brabant", "5611 AZ", 1, 100),
    ("Grote Markt", "Groningen", "Groningen", "9712 HN", 1, 60),
    ("Gedempte Zuiderdiep", "Groningen", "Groningen", "9711 HG", 1, 100),
    ("Hereplein", "Groningen", "Groningen", "9711 GA", 1, 80),
    ("Vrijthof", "Maastricht", "Limburg", "6211 LC", 1, 30),
    ("Markt", "Maastricht", "Limburg", "6211 CL", 1, 80),
    ("Breestraat", "Leiden", "Zuid-Holland", "2311 CS", 1, 150),
    ("Stationsweg", "Leiden", "Zuid-Holland", "2312 AV", 1, 100),
    ("Markt", "Delft", "Zuid-Holland", "2611 GP", 1, 60),
    ("Oude Delft", "Delft", "Zuid-Holland", "2611 CG", 1, 180),
    ("Grote Houtstraat", "Haarlem", "Noord-Holland", "2011 SJ", 1, 180),
    ("Stationsplein", "Haarlem", "Noord-Holland", "2011 LR", 1, 20),
    ("Jansplein", "Arnhem", "Gelderland", "6811 GD", 1, 80),
    ("Nieuwe Oeverstraat", "Arnhem", "Gelderland", "6811 JB", 1, 100),
    ("Grote Markt", "Nijmegen", "Gelderland", "6511 KA", 1, 80),
    ("Keizer Karelplein", "Nijmegen", "Gelderland", "6511 NH", 1, 50),
    ("Heuvelring", "Tilburg", "Noord-Brabant", "5038 CL", 1, 120),
    ("Spoorlaan", "Tilburg", "Noord-Brabant", "5038 CC", 1, 400),
    ("Grote Markt", "Breda", "Noord-Brabant", "4811 XR", 1, 80),
    ("Claudius Prinsenlaan", "Breda", "Noord-Brabant", "4811 DJ", 1, 200),
    ("Grote Markt", "Zwolle", "Overijssel", "8011 LW", 1, 80),
    ("Stadhuisplein", "Amersfoort", "Utrecht", "3811 LM", 1, 30),
]

_DUTCH_FIRST_NAMES = [
    "Daan", "Lucas", "Sem", "Milan", "Lars", "Finn", "Bram", "Thijs", "Jesse", "Sven",
    "Niels", "Joris", "Ruben", "Tim", "Wout", "Pieter", "Thomas", "Joost", "Max", "Timo",
    "Sophie", "Emma", "Julia", "Lisa", "Anna", "Lotte", "Eva", "Noa", "Fleur", "Sara",
    "Nora", "Tess", "Sanne", "Iris", "Mila", "Eline", "Roos", "Maud", "Lieke", "Evi",
]
_DUTCH_LAST_NAMES = [
    "de Vries", "Jansen", "van den Berg", "van Dijk", "Bakker", "Janssen", "Visser",
    "Smit", "Meijer", "de Boer", "Mulder", "de Groot", "Bos", "Vos", "Peters", "Hendriks",
    "van Leeuwen", "Dekker", "Brouwer", "de Wit", "Dijkstra", "Smits", "Kok", "Vermeulen",
    "van der Meer", "van der Linden", "Schouten", "Kramer", "van Dam", "Prins", "Kuiper",
]

_CITY_ALIASES = {
    "amsterdam": "Amsterdam", "rotterdam": "Rotterdam", "the hague": "Den Haag",
    "den haag": "Den Haag", "s-gravenhage": "Den Haag", "utrecht": "Utrecht",
    "eindhoven": "Eindhoven", "groningen": "Groningen", "maastricht": "Maastricht",
    "leiden": "Leiden", "delft": "Delft", "haarlem": "Haarlem", "arnhem": "Arnhem",
    "nijmegen": "Nijmegen", "tilburg": "Tilburg", "breda": "Breda", "zwolle": "Zwolle",
    "amersfoort": "Amersfoort",
}


def _normalize_city(city: str) -> str:
    return _CITY_ALIASES.get(str(city or "").strip().casefold(), "")


def _pick_street(rng: random.Random, geo_city: str = "") -> tuple[str, str, str, str, int, int]:
    normalized = _normalize_city(geo_city)
    candidates = [row for row in _NL_STREETS if row[1] == normalized] if normalized else []
    return rng.choice(candidates or _NL_STREETS)


def generate_nl_billing(
    email: str = "",
    *,
    country: str = "NL",
    currency: str = "EUR",
    seed: Optional[str] = None,
    geo_city: str = "",
) -> dict:
    """Return a fresh NL billing identity suitable for Stripe iDEAL.

    ``seed`` is intentionally opt-in for repeatable tests.  Production calls
    leave it unset so name, street, city and house number all rotate.
    """
    del currency  # iDEAL is EUR-only; keep the argument for provider symmetry.
    rng: random.Random = random.Random(seed) if seed is not None else random.SystemRandom()
    street, city, province, postal, house_min, house_max = _pick_street(rng, geo_city)
    first = rng.choice(_DUTCH_FIRST_NAMES)
    last = rng.choice(_DUTCH_LAST_NAMES)
    name = first + " " + last
    house = rng.randint(house_min, house_max)
    # Keep the billing line in the ordinary Dutch "number street" order.
    line1 = str(house) + " " + street
    account_email = str(email or "").strip()
    billing_email = account_email or (first.lower() + "." + re.sub(r"[^a-z]", "", last.lower()) + str(rng.randint(1000, 9999)) + "@gmail.com")
    return {
        "name": name,
        "email": billing_email,
        "phone": "",
        "tax_id": "",
        "address": {
            "country": str(country or "NL").upper(),
            "line1": line1,
            "line2": "",
            "city": city,
            "state": province,
            "postal_code": postal,
        },
        "_place_name": street,
        "_geo_city_matched": bool(_normalize_city(geo_city) == city),
        "_source": "builtin_public_nl_street",
    }


__all__ = ["generate_nl_billing"]
