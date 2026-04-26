from __future__ import annotations

import json
import os
import re
from typing import Any

import requests
from openai import OpenAI


class AIInterpretationError(Exception):
    """Raised when the AI agent cannot parse or map a request."""


SYSTEM_PROMPT = """
You are an assistant that converts natural-language F5 BIG-IP and BIG-IQ read-only operations questions into JSON.

Valid actions:
- get_pool_members
- get_vip_details
- get_nodes
- reverse_lookup
- generic_chat

Rules:
- This assistant is strictly read-only.
- Never map requests that would change configuration or operational state.
- Examples of blocked intents: create, delete, update, modify, patch, deploy, attach, detach, enable, disable, force offline, force online, restart, sync, save, apply, add, remove.
- Return JSON only.
- Use this schema:
  {
    "action": "<valid action>",
    "node": "<node name or null>",
    "pool": "<pool name or null>",
    "virtual_server": "<virtual server name or null>",
    "target": "<target IP or hostname or null>",
    "reasoning": "<brief explanation or conversational response>"
  }
- Set "pool" only when action is get_pool_members.
- Set "node" only when action is get_nodes for a specific node.
- Set "virtual_server" only when action is get_vip_details for one specific VIP.
- Set "target" only when action is reverse_lookup for an IP or hostname.
- If the user asks a general question or makes conversation, set "action" to "generic_chat" and put your response in "reasoning".
- If the request is an F5 command but ambiguous or unsupported, return:
  {
    "action": "unsupported",
    "node": null,
    "pool": null,
    "virtual_server": null,
    "target": null,
    "reasoning": "<what is missing or unsupported>"
  }
""".strip()


WRITE_INTENT_PATTERNS = [
    r"\badd\b",
    r"\bapply\b",
    r"\battach\b",
    r"\bcreate\b",
    r"\bdelete\b",
    r"\bdeploy\b",
    r"\bdetach\b",
    r"\bdisable\b",
    r"\bdrain\b",
    r"\bedit\b",
    r"\benable\b",
    r"\bforce\s+offline\b",
    r"\bforce\s+online\b",
    r"\bmodify\b",
    r"\bpatch\b",
    r"\breconfigure\b",
    r"\bremove\b",
    r"\brestart\b",
    r"\bsave(?:\s+config(?:uration)?)?\b",
    r"\b(?:run|perform|start|trigger)\s+sync\b",
    r"\bupdate\b",
]


class AIAgent:
    """Intent parser backed by Ollama by default with optional OpenAI support."""

    def __init__(
        self,
        openai_model: str | None = None,
        ollama_model: str | None = None,
        ollama_base_url: str | None = None,
        provider: str | None = None,
    ) -> None:
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_model = openai_model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.ollama_model = ollama_model or os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")
        self.ollama_base_url = (ollama_base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.provider = (provider or os.getenv("AI_PROVIDER", "ollama")).strip().lower()

    def parse_user_query(self, user_query: str) -> dict[str, Any]:
        if not user_query.strip():
            raise AIInterpretationError("User query is empty.")
        if self._is_write_request(user_query):
            return {
                "action": "unsupported",
                "node": None,
                "pool": None,
                "virtual_server": None,
                "target": None,
                "reasoning": "This assistant is read-only and cannot modify F5 configuration or operational state.",
            }

        local_intent = self._parse_local_intent(user_query)
        if local_intent:
            return local_intent

        if self.provider == "openai":
            if not self.openai_api_key:
                raise AIInterpretationError("AI_PROVIDER is set to openai, but OPENAI_API_KEY is not configured.")
            return self._parse_with_openai(user_query)

        if self.provider != "ollama":
            raise AIInterpretationError(f"Unsupported AI_PROVIDER: {self.provider}")

        return self._parse_with_ollama(user_query)

    @staticmethod
    def _is_write_request(user_query: str) -> bool:
        normalized_query = " ".join(user_query.strip().lower().split())
        return any(re.search(pattern, normalized_query) for pattern in WRITE_INTENT_PATTERNS)

    def _parse_local_intent(self, user_query: str) -> dict[str, Any] | None:
        normalized_query = " ".join(user_query.strip().lower().split())

        # Bypass local parser for "how-to" or meta-questions so the LLM can handle them
        if re.search(r"\b(?:how\s+(?:do|to|can|would)|what\s+(?:to|do|should|would)|explain)\b", normalized_query):
            return None

        non_object_names = {
            "all", "one", "only", "single", "specific", "status", "not", "the", "a", "an", "contains", "like", 
            "name", "named", "with", "or", "is", "me", "show", "get", "pull", "check", "config", "configuration", 
            "cofiguration", "details", "pool", "node", "vip", "virtual", "server", "members", "member", "now"
        }

        reverse_lookup_match = re.search(
        r"\b(?:find|where\s+is|reverse\s+lookup|lookup)\s+([/\w.\-:]+)\b",
            user_query,
            re.IGNORECASE,
        )
        if reverse_lookup_match and reverse_lookup_match.group(1).lower() not in non_object_names:
            return {
                "action": "reverse_lookup",
                "node": None,
                "pool": None,
                "virtual_server": None,
                "target": reverse_lookup_match.group(1),
                "reasoning": "The user asked for a reverse lookup of an IP or hostname.",
            }

        pool_match = re.search(
            r"\b(?:status\s+(?:for|of)\s+)?pool(?:\s+members?)?(?:\s+status)?(?:\s+(?:for|of))?\s+([/\w.\-:]+)\b",
            user_query,
            re.IGNORECASE,
        )
        if pool_match and pool_match.group(1).lower() not in non_object_names:
            return {
                "action": "get_pool_members",
                "node": None,
                "pool": pool_match.group(1),
                "virtual_server": None,
                "target": None,
                "reasoning": "The user asked for members of a specific pool.",
            }

        prefix_vip_match = re.search(
            r"\b(?:status|config(?:uration)?|cofiguration|details)\s+(?:for|of)\s+(?:vip|virtual(?:\s+server)?|vs)\s+([/\w.\-:]+)\b",
            user_query,
            re.IGNORECASE,
        )
        if prefix_vip_match and prefix_vip_match.group(1).lower() not in non_object_names:
            return {
                "action": "get_vip_details",
                "node": None,
                "pool": None,
                "virtual_server": prefix_vip_match.group(1),
                "target": None,
                "reasoning": "The user asked for configuration details for a specific VIP.",
            }

        specific_vip_match = re.search(
            r"\b(?:vip|virtual(?:\s+server)?|vs)\s+(?:(?:status|config(?:uration)?|cofiguration|details)\s+(?:for|of)\s+)?([/\w.\-:]+)\b",
            user_query,
            re.IGNORECASE,
        )
        if specific_vip_match and specific_vip_match.group(1).lower() not in non_object_names:
            return {
                "action": "get_vip_details",
                "node": None,
                "pool": None,
                "virtual_server": specific_vip_match.group(1),
                "target": None,
                "reasoning": "The user asked for configuration details for a specific VIP.",
            }

        trailing_vip_match = re.search(
            r"\b(?:show|get|check|pull)\s+(?:(?:status|config(?:uration)?|cofiguration|details)\s+(?:for|of)\s+)?([/\w.\-:]+)\s+(?:vip|virtual(?:\s+server)?|vs)\b",
            user_query,
            re.IGNORECASE,
        )
        if trailing_vip_match and trailing_vip_match.group(1).lower() not in non_object_names:
            return {
                "action": "get_vip_details",
                "node": None,
                "pool": None,
                "virtual_server": trailing_vip_match.group(1),
                "target": None,
                "reasoning": "The user asked for configuration details for a specific VIP.",
            }

        asks_for_single_vip = re.search(r"\b(?:one|single|specific)\b", normalized_query) and re.search(
            r"\b(?:vip|virtual(?: server)?)\b", normalized_query
        )
        if asks_for_single_vip:
            return {
                "action": "unsupported",
                "node": None,
                "pool": None,
                "virtual_server": None,
                "target": None,
                "reasoning": "Please provide the VIP or virtual server name, for example: get VIP status for app1_vs.",
            }

        specific_node_match = re.search(
            r"\b(?:node\s+(?:status\s+(?:for|of)\s+)?|status\s+(?:for|of)\s+node\s+)([/\w.\-:]+)\b",
            user_query,
            re.IGNORECASE,
        )
        if specific_node_match and specific_node_match.group(1).lower() not in non_object_names:
            return {
                "action": "get_nodes",
                "node": specific_node_match.group(1),
                "pool": None,
                "virtual_server": None,
                "target": None,
                "reasoning": "The user asked for one specific node status.",
            }

        implicit_object_match = re.search(
            r"\b(?:status|config(?:uration)?|cofiguration|details)\s+(?:for|of)\s+([/\w.\-:]+)\b",
            user_query,
            re.IGNORECASE,
        )
        if implicit_object_match and implicit_object_match.group(1).lower() not in non_object_names:
            candidate = implicit_object_match.group(1)
            if "pool" in candidate.lower():
                return {
                    "action": "get_pool_members",
                    "node": None,
                    "pool": candidate,
                    "virtual_server": None,
                    "target": None,
                    "reasoning": "Inferred pool configuration request.",
                }
            if "node" in candidate.lower():
                return {
                    "action": "get_nodes",
                    "node": candidate,
                    "pool": None,
                    "virtual_server": None,
                    "target": None,
                    "reasoning": "Inferred node configuration request.",
                }
            return {
                "action": "get_vip_details",
                "node": None,
                "pool": None,
                "virtual_server": candidate,
                "target": None,
                "reasoning": "Inferred VIP configuration details request.",
            }

        return None

    def _parse_with_openai(self, user_query: str) -> dict[str, Any]:
        try:
            client = OpenAI(api_key=self.openai_api_key)
            response = client.chat.completions.create(
                model=self.openai_model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_query},
                ],
            )
            raw_output = response.choices[0].message.content or "{}"
        except Exception as exc:
            raise AIInterpretationError(f"OpenAI request failed: {exc}") from exc

        return self._coerce_json(raw_output)

    def _parse_with_ollama(self, user_query: str) -> dict[str, Any]:
        payload = {
            "model": self.ollama_model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_query},
            ],
        }

        try:
            response = requests.post(
                f"{self.ollama_base_url}/api/chat",
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            body = response.json()
            raw_output = body["message"]["content"]
        except requests.RequestException as exc:
            raise AIInterpretationError(
                "Ollama request failed. Start Ollama locally or set AI_PROVIDER=openai with OPENAI_API_KEY."
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise AIInterpretationError(f"Unexpected Ollama response format: {exc}") from exc

        return self._coerce_json(raw_output)

    def _coerce_json(self, raw_output: str) -> dict[str, Any]:
        try:
            data = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise AIInterpretationError(f"Model returned invalid JSON: {raw_output}") from exc

        action = data.get("action")
        if action not in {"get_pool_members", "get_vip_details", "get_nodes", "reverse_lookup", "unsupported", "generic_chat"}:
            raise AIInterpretationError(f"Unsupported action returned by model: {action}")

        return {
            "action": action,
            "node": data.get("node"),
            "pool": data.get("pool"),
            "virtual_server": data.get("virtual_server"),
            "target": data.get("target"),
            "reasoning": data.get("reasoning", ""),
        }
