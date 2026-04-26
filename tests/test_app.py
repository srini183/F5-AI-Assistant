import os
import unittest
from unittest.mock import patch

from app import parse_and_execute, bigip_is_configured


class AppFlowTests(unittest.TestCase):
    def test_unsupported_request_does_not_get_default_target(self) -> None:
        action_payload, result = parse_and_execute("disable pool member app1")

        self.assertEqual(action_payload["action"], "unsupported")
        self.assertIn("read-only", result["message"])

    def test_placeholder_bigip_is_not_configured(self) -> None:
        env = {
            "BIGIP_HOST": "https://your-bigip.example.com",
            "BIGIP_USERNAME": "admin",
            "BIGIP_PASSWORD": "secret",
        }

        with patch.dict(os.environ, env, clear=True):
            # Ensure we also hide devices.json if it exists locally during test
            with patch('os.path.exists', return_value=False):
                self.assertFalse(bigip_is_configured())


if __name__ == "__main__":
    unittest.main()
