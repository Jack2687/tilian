"""Generate explicitly synthetic UPI billing fixtures without network access.

These fixtures are for offline/sandbox testing only. Geographic references are
test scenarios, not verified billing addresses or evidence of payment success.
This standalone tool does not load application settings or production modules.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import Any


ENGINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCATIONS = ENGINE_ROOT / "tests" / "fixtures" / "upi_sandbox" / "locations.json"
ALLOWED_STATES = {
    "DL": "Delhi",
    "KA": "Karnataka",
    "MH": "Maharashtra",
    "TN": "Tamil Nadu",
    "WB": "West Bengal",
}
WEIGHTS_BY_BASIS = {
    "baseline": 1,
    "user_reported_city_pin": 3,
    "user_reported_full_address": 5,
}
SYNTHETIC_LINE2 = "Synthetic fixture; not a billing address"
RECORD_FIELDS = {
    "fixture_id", "test_only", "environment", "name", "email", "address",
    "test_sampling_weight", "weight_basis", "reference_ids", "location_id",
}
ADDRESS_FIELDS = {"country", "state", "state_code", "city", "postal_code", "line1", "line2"}


def _positive_count(count: Any) -> int:
    if type(count) is not int or count < len(ALLOWED_STATES):
        raise ValueError("count must be an integer of at least 5 (one record per state)")
    return count


def _validate_config(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("locations config must have schema_version 1")
    if config.get("allowed_states") != ALLOWED_STATES:
        raise ValueError("allowed_states must contain exactly the five supported states")
    locations = config.get("locations")
    if not isinstance(locations, list) or not locations:
        raise ValueError("locations must be a nonempty list")
    if not isinstance(config.get("references"), list):
        raise ValueError("references must be a list")

    by_id: dict[str, dict[str, Any]] = {}
    state_codes = set()
    for location in locations:
        if not isinstance(location, dict):
            raise ValueError("every location must be an object")
        location_id = location.get("location_id")
        if not isinstance(location_id, str) or not location_id.strip() or location_id in by_id:
            raise ValueError("location_id must be a nonempty unique string")
        state_code = location.get("state_code")
        if not isinstance(state_code, str) or state_code not in ALLOWED_STATES:
            raise ValueError(f"unsupported state_code for {location_id}")
        if location.get("state") != ALLOWED_STATES[state_code]:
            raise ValueError(f"state name does not match state_code for {location_id}")
        city = location.get("city")
        if not isinstance(city, str) or not city.strip() or city != city.strip():
            raise ValueError(f"city must be a nonempty trimmed string for {location_id}")
        postal_code = location.get("postal_code")
        if not isinstance(postal_code, str) or re.fullmatch(r"[1-9][0-9]{5}", postal_code) is None:
            raise ValueError(f"postal_code must be six ASCII digits for {location_id}")
        weight = location.get("test_sampling_weight")
        basis = location.get("weight_basis")
        if (
            type(weight) is not int
            or not isinstance(basis, str)
            or basis not in WEIGHTS_BY_BASIS
            or weight != WEIGHTS_BY_BASIS[basis]
        ):
            raise ValueError(f"test_sampling_weight and weight_basis do not agree for {location_id}")
        reference_ids = location.get("reference_ids")
        if (
            not isinstance(reference_ids, list)
            or any(not isinstance(value, str) or not value.strip() for value in reference_ids)
            or len(reference_ids) != len(set(reference_ids))
        ):
            raise ValueError(f"reference_ids must contain unique nonempty strings for {location_id}")
        by_id[location_id] = location
        state_codes.add(state_code)
    if state_codes != set(ALLOWED_STATES):
        raise ValueError("at least one source location is required for each of the five states")
    return by_id


def load_locations(path: str | Path = DEFAULT_LOCATIONS) -> dict[str, Any]:
    """Read and validate the explicit geographic test scenarios."""
    with Path(path).open("r", encoding="utf-8-sig") as source:
        config = json.load(source)
    _validate_config(config)
    return config


def generate_records(config: dict[str, Any], count: int = 3000) -> list[dict[str, Any]]:
    """Create deterministic records, balancing states and rotating their locations.

    Weights are metadata for subsequent test sampling; they do not alter the
    balanced fixture allocation and must not be interpreted as success rates.
    """
    _positive_count(count)
    _validate_config(config)
    records: list[dict[str, Any]] = []
    base_count, remainder = divmod(count, len(ALLOWED_STATES))
    for state_index, state_code in enumerate(ALLOWED_STATES):
        locations = [item for item in config["locations"] if item["state_code"] == state_code]
        state_count = base_count + int(state_index < remainder)
        for local_index in range(state_count):
            location = locations[local_index % len(locations)]
            serial = f"{len(records) + 1:06d}"
            records.append({
                "fixture_id": f"UPI-SANDBOX-{serial}",
                "test_only": True,
                "environment": "sandbox",
                "name": f"Sandbox Customer {serial}",
                "email": f"upi-{serial}@example.invalid",
                "address": {
                    "country": "IN",
                    "state": location["state"],
                    "state_code": state_code,
                    "city": location["city"],
                    "postal_code": location["postal_code"],
                    "line1": f"SANDBOX ONLY - Test Street {serial}",
                    "line2": SYNTHETIC_LINE2,
                },
                "test_sampling_weight": location["test_sampling_weight"],
                "weight_basis": location["weight_basis"],
                "reference_ids": list(location["reference_ids"]),
                "location_id": location["location_id"],
            })
    return records


def _dataset_bytes(records: list[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")


def validate_records(
    records: list[dict[str, Any]],
    config: dict[str, Any],
    expected_count: int | None = None,
) -> dict[str, Any]:
    """Reject modified/non-synthetic fixtures and return a reproducible summary."""
    by_id = _validate_config(config)
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    _positive_count(len(records))
    if expected_count is not None:
        _positive_count(expected_count)
        if len(records) != expected_count:
            raise ValueError(f"expected {expected_count} records, found {len(records)}")

    fixture_ids: set[str] = set()
    address_keys: set[tuple[str, ...]] = set()
    state_counts: Counter[str] = Counter()
    weight_counts: Counter[int] = Counter()
    location_counts: Counter[str] = Counter()
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
            raise ValueError(f"record {index} has missing or unexpected fields")
        fixture_id = record["fixture_id"]
        if not isinstance(fixture_id, str) or re.fullmatch(r"UPI-SANDBOX-[0-9]{6,}", fixture_id) is None:
            raise ValueError(f"record {index} has an invalid fixture_id")
        serial = fixture_id.removeprefix("UPI-SANDBOX-")
        number = int(serial)
        if not 1 <= number <= len(records) or serial != f"{number:06d}":
            raise ValueError(f"record {index} has a noncanonical fixture_id")
        if fixture_id in fixture_ids:
            raise ValueError(f"duplicate fixture_id: {fixture_id}")
        fixture_ids.add(fixture_id)
        if record["test_only"] is not True or record["environment"] != "sandbox":
            raise ValueError(f"record {index} must be explicitly marked sandbox/test_only")
        if record["name"] != f"Sandbox Customer {serial}" or record["email"] != f"upi-{serial}@example.invalid":
            raise ValueError(f"record {index} must use the synthetic name and reserved email domain")
        location_id = record["location_id"]
        if not isinstance(location_id, str) or location_id not in by_id:
            raise ValueError(f"record {index} refers to an unknown location")
        location = by_id[location_id]
        address = record["address"]
        if not isinstance(address, dict) or set(address) != ADDRESS_FIELDS:
            raise ValueError(f"record {index} has missing or unexpected address fields")
        expected_address = {
            "country": "IN",
            "state": location["state"],
            "state_code": location["state_code"],
            "city": location["city"],
            "postal_code": location["postal_code"],
            "line1": f"SANDBOX ONLY - Test Street {serial}",
            "line2": SYNTHETIC_LINE2,
        }
        if address != expected_address:
            raise ValueError(f"record {index} has non-synthetic address lines or mismatched location fields")
        for field in ("test_sampling_weight", "weight_basis", "reference_ids"):
            if record[field] != location[field]:
                raise ValueError(f"record {index} has a mismatched {field}")
        if type(record["test_sampling_weight"]) is not int:
            raise ValueError(f"record {index} weight must be an integer")
        address_key = tuple(address[field] for field in sorted(ADDRESS_FIELDS))
        if address_key in address_keys:
            raise ValueError(f"record {index} has a duplicate address")
        address_keys.add(address_key)
        state_counts[location["state_code"]] += 1
        weight_counts[record["test_sampling_weight"]] += 1
        location_counts[location_id] += 1

    if set(state_counts) != set(ALLOWED_STATES) or max(state_counts.values()) - min(state_counts.values()) > 1:
        raise ValueError("records must be evenly distributed across exactly the five supported states")

    return {
        "schema_version": 1,
        "test_only": True,
        "synthetic": True,
        "environment": "sandbox",
        "notice": (
            "Synthetic offline/sandbox fixtures only. Address lines and contacts are fictional. "
            "Geographic references are not address verification or a guarantee of payment success. "
            "Test sampling weights are scenario labels, not measured success probabilities."
        ),
        "record_count": len(records),
        "unique_fixture_ids": len(fixture_ids),
        "unique_addresses": len(address_keys),
        "duplicates": 0,
        "state_counts": {state: state_counts[code] for code, state in ALLOWED_STATES.items()},
        "state_code_counts": {code: state_counts[code] for code in ALLOWED_STATES},
        "weight_distribution": {str(weight): weight_counts[weight] for weight in (1, 3, 5)},
        "source_location_count": len(by_id),
        "used_location_count": len(location_counts),
        "location_counts": {location_id: location_counts[location_id] for location_id in by_id},
        "dataset_sha256": hashlib.sha256(_dataset_bytes(records)).hexdigest(),
    }


def write_dataset(
    records: list[dict[str, Any]],
    config: dict[str, Any],
    output_dir: str | Path,
    expected_count: int | None = None,
) -> dict[str, Any]:
    """Validate the entire dataset before creating or writing any output files."""
    summary = validate_records(records, config, expected_count=expected_count)
    dataset = _dataset_bytes(records)
    summary_data = (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "addresses.jsonl").write_bytes(dataset)
    (destination / "summary.json").write_bytes(summary_data)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--locations", type=Path, default=DEFAULT_LOCATIONS, help="Geographic scenario JSON")
    parser.add_argument("--output-dir", type=Path, help="Output directory (defaults to the locations file's directory)")
    parser.add_argument("--count", type=int, default=3000, help="Record count, at least 5 (default: 3000)")
    args = parser.parse_args(argv)
    destination = args.output_dir if args.output_dir is not None else args.locations.parent
    try:
        if args.locations.resolve() in {
            (destination / "addresses.jsonl").resolve(),
            (destination / "summary.json").resolve(),
        }:
            raise ValueError("output files must not overwrite the locations input")
        config = load_locations(args.locations)
        records = generate_records(config, count=args.count)
        summary = write_dataset(records, config, destination, expected_count=args.count)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"Generated {summary['record_count']} synthetic sandbox fixtures in {destination.resolve()}")
    print(f"SHA-256: {summary['dataset_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
