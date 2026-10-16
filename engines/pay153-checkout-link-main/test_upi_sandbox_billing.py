from __future__ import annotations

from collections import Counter
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


ENGINE_ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "generate_upi_sandbox_billing", ENGINE_ROOT / "tools" / "generate_upi_sandbox_billing.py"
)
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)
LOCATIONS_PATH = ENGINE_ROOT / "tests" / "fixtures" / "upi_sandbox" / "locations.json"
EXPECTED_STATES = {
    "DL": "Delhi", "KA": "Karnataka", "MH": "Maharashtra",
    "TN": "Tamil Nadu", "WB": "West Bengal",
}


class SandboxBillingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for target in ("socket.create_connection", "socket.socket.connect", "socket.getaddrinfo"):
            blocker = patch(target, side_effect=AssertionError("Sandbox fixtures must remain offline"))
            blocker.start()
            cls.addClassCleanup(blocker.stop)
        cls.config = generator.load_locations(LOCATIONS_PATH)
        cls.records = generator.generate_records(cls.config)

    def test_default_dataset_has_3000_unique_records_and_600_per_state(self):
        summary = generator.validate_records(self.records, self.config, expected_count=3000)
        self.assertEqual(len(self.records), 3000)
        self.assertEqual(len({record["fixture_id"] for record in self.records}), 3000)
        self.assertEqual(len({tuple(sorted(record["address"].items())) for record in self.records}), 3000)
        self.assertEqual(
            Counter(record["address"]["state"] for record in self.records),
            {state: 600 for state in EXPECTED_STATES.values()},
        )
        self.assertEqual(summary["record_count"], 3000)
        self.assertEqual(summary["unique_fixture_ids"], 3000)
        self.assertEqual(summary["unique_addresses"], 3000)
        self.assertEqual(summary["duplicates"], 0)
        self.assertIs(summary["test_only"], True)
        self.assertIs(summary["synthetic"], True)
        self.assertEqual(summary["environment"], "sandbox")

    def test_all_records_remain_clearly_synthetic(self):
        for record in self.records:
            self.assertIs(record["test_only"], True)
            self.assertEqual(record["environment"], "sandbox")
            self.assertRegex(record["name"], r"^Sandbox Customer [0-9]{6}$")
            self.assertRegex(record["email"], r"^upi-[0-9]{6}@example\.invalid$")
            self.assertRegex(record["address"]["line1"], r"^SANDBOX ONLY - Test Street [0-9]{6}$")
            self.assertEqual(record["address"]["line2"], "Synthetic fixture; not a billing address")
            self.assertEqual(record["address"]["country"], "IN")

    def test_default_dataset_covers_every_configured_location_without_mixing_fields(self):
        sources = {location["location_id"]: location for location in self.config["locations"]}
        self.assertEqual({record["location_id"] for record in self.records}, set(sources))
        for record in self.records:
            source = sources[record["location_id"]]
            for field in ("city", "state", "state_code", "postal_code"):
                self.assertEqual(record["address"][field], source[field])
            self.assertEqual(record["address"]["state"], EXPECTED_STATES[record["address"]["state_code"]])
            for field in ("test_sampling_weight", "weight_basis", "reference_ids"):
                self.assertEqual(record[field], source[field])

    def test_other_states_stay_in_reference_audit_only(self):
        excluded_ids = {
            reference["reference_id"] for reference in self.config["references"]
            if not reference["included_in_sandbox"]
        }
        self.assertEqual(excluded_ids, {"T03", "T04", "T06", "T07", "T08", "T11"})
        generated_reference_ids = {ref for record in self.records for ref in record["reference_ids"]}
        self.assertFalse(excluded_ids & generated_reference_ids)
        excluded_cities = {"Lucknow", "Vijayawada", "Ahmedabad", "Kochi", "Thiruvananthapuram"}
        self.assertFalse(excluded_cities & {record["address"]["city"] for record in self.records})

    def test_delhi_is_normalized_while_preserving_original_reference(self):
        reference = next(item for item in self.config["references"] if item["reference_id"] == "T05")
        self.assertEqual(reference["city_original"], "Delhi")
        self.assertEqual(reference["city"], "New Delhi")
        records = [record for record in self.records if "T05" in record["reference_ids"]]
        self.assertTrue(records)
        for record in records:
            address = record["address"]
            self.assertEqual(
                (address["city"], address["state"], address["state_code"], address["postal_code"]),
                ("New Delhi", "Delhi", "DL", "110001"),
            )

    def test_success_reports_do_not_become_verification_claims(self):
        self.assertTrue(all(
            item["verification_status"] == "user_reported_unverified"
            for item in self.config["references"]
        ))
        self.assertTrue(all(
            item["verification_status"] == "not_postally_verified"
            for item in self.config["locations"]
        ))
        self.assertEqual({record["test_sampling_weight"] for record in self.records}, {1, 3, 5})

    def test_duplicate_success_reports_do_not_multiply_weights(self):
        for postal_code, reference_ids, weight in (
            ("641018", {"T09", "T10"}, 3),
            ("625001", {"F03", "F05"}, 5),
        ):
            with self.subTest(postal_code=postal_code):
                locations = [item for item in self.config["locations"] if item["postal_code"] == postal_code]
                self.assertEqual(len(locations), 1)
                self.assertEqual(set(locations[0]["reference_ids"]), reference_ids)
                self.assertEqual(locations[0]["test_sampling_weight"], weight)

    def test_arbitrary_supported_counts_are_balanced(self):
        for count in (5, 6, 7, 9, 10, 37, 3001):
            with self.subTest(count=count):
                records = generator.generate_records(self.config, count=count)
                generator.validate_records(records, self.config, expected_count=count)
                counts = Counter(record["address"]["state_code"] for record in records)
                self.assertEqual(len(records), count)
                self.assertEqual(set(counts), set(EXPECTED_STATES))
                self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_generation_is_reproducible_and_does_not_mutate_config(self):
        original = copy.deepcopy(self.config)
        repeated = generator.generate_records(self.config)
        self.assertEqual(repeated, self.records)
        self.assertEqual(self.config, original)
        first_summary = generator.validate_records(self.records, self.config)
        self.assertEqual(first_summary, generator.validate_records(repeated, self.config))
        self.assertRegex(first_summary["dataset_sha256"], r"^[a-f0-9]{64}$")
        repeated[0]["reference_ids"].append("unrelated-reference")
        self.assertEqual(self.config, original)
        self.assertNotEqual(repeated[0]["reference_ids"], self.records[0]["reference_ids"])

    def test_invalid_counts_are_rejected(self):
        for count in (0, -1, 4, True, False, 5.0, "3000", None):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    generator.generate_records(self.config, count=count)

    def test_duplicate_record_and_count_mismatch_are_rejected(self):
        records = generator.generate_records(self.config, count=10)
        records[1] = copy.deepcopy(records[0])
        with self.assertRaises(ValueError):
            generator.validate_records(records, self.config)
        with self.assertRaises(ValueError):
            generator.validate_records(self.records, self.config, expected_count=3001)

    def test_removed_sandbox_markers_are_rejected(self):
        for field in ("test_only", "environment"):
            with self.subTest(field=field):
                records = generator.generate_records(self.config, count=10)
                del records[0][field]
                with self.assertRaises(ValueError):
                    generator.validate_records(records, self.config)

    def test_tampered_identity_geography_and_sampling_metadata_are_rejected(self):
        mutations = (
            ("test_only", False), ("test_only", 1), ("environment", "production"),
            ("name", "Actual Customer"), ("email", "customer@gmail.com"),
            ("location_id", "unknown-location"), ("test_sampling_weight", 99),
            ("test_sampling_weight", 3.0), ("weight_basis", "verified_success"),
            ("reference_ids", ["T03"]),
            ("address.line1", "140 Pawasakar Galli Road, Somwar Peth"),
            ("address.line2", "Unit 12"), ("address.postal_code", "400017"),
            ("address.state_code", "MH"), ("address.state", "Maharashtra"),
            ("address.city", "Mumbai"), ("address.country", "US"),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                records = generator.generate_records(self.config, count=10)
                if field.startswith("address."):
                    records[0]["address"][field.split(".")[1]] = value
                else:
                    records[0][field] = value
                with self.assertRaises(ValueError):
                    generator.validate_records(records, self.config)

    def test_out_of_scope_or_incomplete_source_config_is_rejected(self):
        for mutation in ("unsupported_state", "missing_state", "duplicate_location", "invalid_weight"):
            with self.subTest(mutation=mutation):
                config = copy.deepcopy(self.config)
                if mutation == "unsupported_state":
                    config["locations"][0]["state_code"] = "UP"
                    config["locations"][0]["state"] = "Uttar Pradesh"
                elif mutation == "missing_state":
                    config["locations"] = [item for item in config["locations"] if item["state_code"] != "WB"]
                elif mutation == "duplicate_location":
                    config["locations"].append(copy.deepcopy(config["locations"][0]))
                else:
                    config["locations"][0]["test_sampling_weight"] = 10
                with self.assertRaises(ValueError):
                    generator.generate_records(config)


if __name__ == "__main__":
    unittest.main()
