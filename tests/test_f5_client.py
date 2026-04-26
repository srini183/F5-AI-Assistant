import unittest

import requests

from f5_client import F5Client, F5ClientError


class FakeResponse:
    def __init__(self, payload=None, json_error: Exception | None = None) -> None:
        self.payload = payload
        self.json_error = json_error

    def raise_for_status(self) -> None:
        return None

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def get(self, url: str, timeout: int) -> FakeResponse:
        return self.response


def build_client(response: FakeResponse) -> F5Client:
    client = F5Client.__new__(F5Client)
    client.host = "https://f5.example.test"
    client.timeout = 30
    client.session = FakeSession(response)
    return client


class F5ClientTests(unittest.TestCase):
    def test_placeholder_host_is_rejected(self) -> None:
        with self.assertRaisesRegex(F5ClientError, "Missing real"):
            F5Client(
                target="bigip",
                host="https://your-bigip.example.com",
                username="admin",
                password="secret",
            )

    def test_bigip_pool_reference_supports_folders_and_encoding(self) -> None:
        self.assertEqual(F5Client._bigip_pool_reference("app1"), "~Common~app1")
        self.assertEqual(
            F5Client._bigip_pool_reference("/Common/app/folder_pool"),
            "~Common~app~folder_pool",
        )
        self.assertEqual(F5Client._bigip_pool_reference("/Common/app pool"), "~Common~app%20pool")

    def test_request_wraps_non_json_response(self) -> None:
        client = build_client(FakeResponse(json_error=ValueError("not json")))

        with self.assertRaisesRegex(F5ClientError, "non-JSON"):
            client._request("/bad")

    def test_request_wraps_unexpected_json_shape(self) -> None:
        client = build_client(FakeResponse(payload=[]))

        with self.assertRaisesRegex(F5ClientError, "unexpected response shape"):
            client._request("/bad")

    def test_request_keeps_http_errors_wrapped(self) -> None:
        class ErrorResponse(FakeResponse):
            def raise_for_status(self) -> None:
                raise requests.HTTPError("boom")

        client = build_client(ErrorResponse(payload={}))

        with self.assertRaisesRegex(F5ClientError, "F5 API request failed"):
            client._request("/bad")

    def test_get_vip_status_filters_specific_virtual_server(self) -> None:
        client = build_client(
            FakeResponse(
                payload={
                    "items": [
                        {
                            "name": "app1_vs",
                            "fullPath": "/Common/app1_vs",
                            "destination": "/Common/10.1.1.10:443",
                            "enabled": True,
                            "availabilityState": "available",
                        },
                        {
                            "name": "app2_vs",
                            "fullPath": "/Common/app2_vs",
                            "destination": "/Common/10.1.1.20:443",
                            "enabled": True,
                            "availabilityState": "offline",
                        },
                    ]
                }
            )
        )
        client.target = "bigiq"

        result = client.get_vip_status("app1_vs")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "app1_vs")


if __name__ == "__main__":
    unittest.main()
