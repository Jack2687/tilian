import json
import unittest

from gcash_engine.sentinel_token import sentinel_header_value


class SentinelTokenTests(unittest.TestCase):
    def test_main_header_keeps_session_observer_out_of_private_fields(self):
        token = {
            "p": "proof",
            "t": "turnstile",
            "c": "challenge",
            "id": "device-1",
            "flow": "checkout_session_approval",
            "_so": {"so": "observer", "c": "challenge", "id": "device-1"},
        }

        encoded = sentinel_header_value(token)

        self.assertNotIn("_so", json.loads(encoded))
        self.assertIn("_so", token)


if __name__ == "__main__":
    unittest.main()
