import unittest

from ai_agent import AIAgent


class AIAgentSafetyTests(unittest.TestCase):
    def test_defaults_to_local_ollama_provider(self) -> None:
        agent = AIAgent()

        self.assertEqual(agent.provider, "ollama")

    def test_openai_requires_explicit_provider(self) -> None:
        agent = AIAgent(provider="openai")

        self.assertEqual(agent.provider, "openai")

    def test_allows_read_only_status_queries(self) -> None:
        read_only_queries = [
            "show offline members",
            "list online virtual servers",
            "show pool sync status",
        ]

        for query in read_only_queries:
            with self.subTest(query=query):
                self.assertFalse(AIAgent._is_write_request(query))

    def test_blocks_write_intent_queries(self) -> None:
        write_queries = [
            "disable pool member app1",
            "force offline node 10.1.1.1",
            "run sync now",
            "update the virtual server",
        ]

        for query in write_queries:
            with self.subTest(query=query):
                self.assertTrue(AIAgent._is_write_request(query))

    def test_local_parser_asks_for_vip_name_when_single_vip_is_ambiguous(self) -> None:
        payload = AIAgent().parse_user_query("i want get one vip status only not all")

        self.assertEqual(payload["action"], "unsupported")
        self.assertIn("VIP", payload["reasoning"])

    def test_local_parser_extracts_specific_vip_name(self) -> None:
        payload = AIAgent().parse_user_query("get VIP status for app1_vs")

        self.assertEqual(payload["action"], "get_vip_details")
        self.assertEqual(payload["virtual_server"], "app1_vs")

    def test_local_parser_extracts_pool_member_name(self) -> None:
        payload = AIAgent().parse_user_query("show pool members for app1")

        self.assertEqual(payload["action"], "get_pool_members")
        self.assertEqual(payload["pool"], "app1")


if __name__ == "__main__":
    unittest.main()
