import os
import unittest
from unittest.mock import patch

from app import get_preferred_target, parse_and_execute, target_is_configured


class AppFlowTests(unittest.TestCase):
    def test_unsupported_request_does_not_get_default_target(self) -> None:
        action_payload, result = parse_and_execute("disable pool member app1", "bigip")

        self.assertEqual(action_payload["action"], "unsupported")
        self.assertIsNone(action_payload["target"])
        self.assertIn("read-only", result["message"])

    def test_placeholder_bigip_is_not_configured(self) -> None:
        env = {
            "BIGIP_HOST": "https://your-bigip.example.com",
            "BIGIP_USERNAME": "admin",
            "BIGIP_PASSWORD": "secret",
        }

        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(target_is_configured("bigip"))

    def test_prefers_bigiq_when_only_bigiq_is_configured(self) -> None:
        env = {
            "BIGIP_HOST": "https://your-bigip.example.com",
            "BIGIP_USERNAME": "admin",
            "BIGIP_PASSWORD": "secret",
            "BIGIQ_HOST": "https://100.102.94.200",
            "BIGIQ_USERNAME": "admin",
            "BIGIQ_PASSWORD": "secret",
        }

        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(get_preferred_target(), "bigiq")


if __name__ == "__main__":
    unittest.main()
