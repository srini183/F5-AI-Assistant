from __future__ import annotations

import concurrent.futures
import csv
import io
import json
import os
from typing import Any

import streamlit as st
from dotenv import load_dotenv

from ai_agent import AIAgent, AIInterpretationError
from f5_client import F5Client, F5ClientError


load_dotenv(override=True)


st.set_page_config(page_title="F5 AI Assistant", page_icon=":material/hub:", layout="wide")


SMALL_TALK_RESPONSES = {
    "hi": "Hi! I can help with read-only F5 BIG-IP queries like `show config of vip <ip/name>` or `check node status for 10.1.1.1`.",
    "hello": "Hello! Ask me a read-only F5 question like `where is 10.1.1.1` or `show config of vip <ip/name>`.",
    "hey": "Hey! I am ready to help with read-only F5 BIG-IP lookups.",
    "how are you": "I am doing well and ready to help with read-only F5 BIG-IP queries.",
    "how are you?": "I am doing well and ready to help with read-only F5 BIG-IP queries.",
    "what can you do": "I can help with read-only F5 lookups like `where is 10.1.1.1`, `show config of vip <ip/name>`, and `check node status for 10.1.1.1`.",
    "what can you do?": "I can help with read-only F5 lookups like `where is 10.1.1.1`, `show config of vip <ip/name>`, and `check node status for 10.1.1.1`.",
    "what other details you can give": "I can fetch read-only details like pools, pool members, virtual server status, and node status from your F5 devices.",
    "what else can you do": "I can fetch read-only details like pools, pool members, virtual server status, and node status from your F5 devices.",
    "help": "I can help with read-only F5 lookups! Try asking me to `show config of vip <ip/name>`, `where is 10.1.1.1`, or `check node status for 10.1.1.1`.",
    "thanks": "You are welcome. Ask another read-only F5 question any time.",
    "thank you": "You are welcome. Ask another read-only F5 question any time.",
}


def initialize_session_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": "Ready for F5 visibility checks. Try `show config of vip <ip/name>` or `where is 10.1.1.1`.",
            }
        ]


def get_small_talk_response(user_query: str) -> str | None:
    normalized_query = " ".join(user_query.strip().lower().split())
    return SMALL_TALK_RESPONSES.get(normalized_query)


def is_configured(*names: str) -> bool:
    return any(is_real_env_value(os.getenv(name, "")) for name in names)


def is_real_env_value(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    if not normalized:
        return False
    return "your-bigip.example.com" not in normalized


def bigip_is_configured() -> bool:
    if os.path.exists(os.path.join(os.path.dirname(__file__), "devices.json")):
        return True
    return (
        is_configured("BIGIP_HOST", "F5_HOST")
        and is_configured("BIGIP_USERNAME", "F5_USERNAME")
        and is_configured("BIGIP_PASSWORD", "F5_PASSWORD")
    )


def status_label(is_ready: bool) -> str:
    return "Ready" if is_ready else "Missing"


def status_class(is_ready: bool) -> str:
    return "status-ready" if is_ready else "status-missing"


def apply_app_styles() -> None:
    css_path = os.path.join(os.path.dirname(__file__), "style.css")
    if os.path.exists(css_path):
        with open(css_path) as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)


def render_header() -> None:
    st.markdown(
        """
        <div class="app-header">
            <h1>F5 AI Assistant</h1>
            <p>Read-only BIG-IP visibility from natural-language prompts.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_status_tiles() -> None:
    bigip_ready = bigip_is_configured()
    parser_ready = is_configured("OLLAMA_BASE_URL") or (
        os.getenv("AI_PROVIDER", "ollama").strip().lower() == "openai" and is_configured("OPENAI_API_KEY")
    )

    st.markdown(
        f"""
        <div class="status-grid">
            <div class="status-tile"><span>BIG-IP</span><strong class="{status_class(bigip_ready)}">{status_label(bigip_ready)}</strong></div>
            <div class="status-tile"><span>Local Parser</span><strong class="{status_class(parser_ready)}">{status_label(parser_ready)}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def query_single_device(host_url: str, action_payload: dict[str, Any]) -> Any:
    try:
        # Increased timeout to 10 seconds. BIG-IP auth token generation often takes 5+ seconds.
        client = F5Client(host=host_url, timeout=10)
        action = action_payload["action"]

        if action == "get_pool_members":
            members = client.get_pool_members(action_payload.get("pool"))
            for m in members:
                m["Source BIG-IP"] = host_url
            return {
                "type": "pool_members",
                "pool_name": action_payload.get("pool"),
                "members": members,
                "device": host_url
            }
        elif action == "get_vip_details":
            result = client.get_vip_details(action_payload.get("virtual_server"))
            result["device"] = host_url
            return result
        elif action == "get_nodes":
            nodes = client.get_nodes(action_payload.get("node"))
            for n in nodes:
                n["Source BIG-IP"] = host_url
            return nodes
        elif action == "reverse_lookup":
            result = client.reverse_lookup(action_payload.get("target"))
            result["device"] = host_url
            return result
    except Exception as e:
        print(f"Warning: Failed to query {host_url} - {str(e)}")
        return {"error": True, "device": host_url, "message": str(e), "type": "error", "target": action_payload.get("target", "")}


def parse_and_execute(user_query: str) -> tuple[dict[str, Any], Any]:
    agent = AIAgent()
    action_payload = agent.parse_user_query(user_query)

    if action_payload["action"] == "unsupported":
        return action_payload, {
            "message": action_payload["reasoning"] or "I could not map that request to a supported F5 action."
        }

    if action_payload["action"] == "generic_chat":
        return action_payload, None

    if not bigip_is_configured():
        return action_payload, {
            "message": "BIG-IP is not configured. Add devices.json or set .env variables."
        }

    action = action_payload["action"]
    if action == "get_pool_members" and not action_payload.get("pool"):
        raise AIInterpretationError("A pool name is required for the get_pool_members action.")
    elif action == "get_nodes" and not action_payload.get("node"):
        raise AIInterpretationError("A specific node name or IP is required to check node status.")
    elif action == "reverse_lookup" and not action_payload.get("target"):
        raise AIInterpretationError("A target IP or hostname is required for reverse lookup.")

    devices_file = os.path.join(os.path.dirname(__file__), "devices.json")
    hosts = []
    if os.path.exists(devices_file):
        with open(devices_file) as f:
            data = json.load(f)
            if isinstance(data, dict):
                hosts = [url for key, url in data.items() if not key.strip().startswith("#")]
            else:
                hosts = data
    else:
        default_host = os.getenv("BIGIP_HOST", os.getenv("F5_HOST", ""))
        if default_host and not F5Client._is_placeholder_host(default_host):
            hosts = [default_host]

    if not hosts:
        return action_payload, {"message": "No BIG-IP hosts found to query."}

    results = []
    errors = []
    not_founds = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(hosts), 50)) as executor:
        futures = [executor.submit(query_single_device, host, action_payload) for host in hosts]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                if isinstance(res, dict) and res.get("type") == "not_found":
                    not_founds.append(res)
                elif isinstance(res, dict) and res.get("error"):
                    errors.append(res)
                else:
                    results.append(res)

    if not results:
        if action == "reverse_lookup":
            if not_founds:
                return action_payload, not_founds[0]
            if errors:
                return action_payload, errors[0]
        if errors:
            return action_payload, {"message": f"{errors[0]['message']}"}
        return action_payload, {"message": "Object not found on any configured BIG-IP devices."}

    if action == "get_nodes":
        combined = []
        for r in results:
            combined.extend(r)
        return action_payload, combined

    return action_payload, results[0]


def render_result(result: Any, key_prefix: str = "latest") -> None:
    if isinstance(result, list) and result:
        # Detect if the list is purely Node Status data to render as a metric dashboard
        is_node_check = "Address" in result[0] and "State" in result[0] and "Availability" not in result[0]

        display_data = [{k: v for k, v in r.items() if k != "Full Configuration"} for r in result]

        if is_node_check:
            for node in result:
                st.markdown(f"### :material/dns: Node Status: `{node.get('Name')}`")
                c1, c2, c3 = st.columns(3)
                c1.write(f"**Address:** `{node.get('Address', 'N/A')}`")
                c2.write(f"**Health State:** {node.get('State', 'Unknown')}")
                c3.write(f"**Admin Session:** {node.get('Session', 'Unknown')}")
                
                if node.get("Full Configuration"):
                    with st.expander(":material/code: View Full Configuration"):
                        st.json(node["Full Configuration"])
                st.divider()
        else:
            st.dataframe(display_data, use_container_width=True)

        if isinstance(result[0], dict):
            csv_data = display_data
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=csv_data[0].keys())
            writer.writeheader()
            writer.writerows(csv_data)
            st.download_button(
                label=":material/download: Download Data as CSV",
                data=output.getvalue(),
                file_name="f5_data.csv",
                mime="text/csv",
                key=f"dl_csv_{key_prefix}",
            )
                
            raw_configs = {r.get("Name", f"Item_{i}"): r.get("Full Configuration") for i, r in enumerate(result) if r.get("Full Configuration")}
            if raw_configs:
                with st.expander(":material/code: View Full Configuration"):
                    st.json(raw_configs)

        return

    if isinstance(result, list):
        st.info("The request completed, but no records were returned.")
        return

    if isinstance(result, dict):
        if set(result) == {"message"}:
            st.info(result["message"])
            return

        if result.get("type") == "pool_members":
            st.markdown(f"### :material/lan: Pool Members: `{result['pool_name']}`")
            if result.get("members"):
                display_members = [{k: v for k, v in m.items() if k != "Full Configuration"} for m in result["members"]]
                st.dataframe(display_members, use_container_width=True)
                
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=display_members[0].keys())
                writer.writeheader()
                writer.writerows(display_members)
                st.download_button(
                    label=":material/download: Download Data as CSV",
                    data=output.getvalue(),
                    file_name=f"{result['pool_name']}_members.csv",
                    mime="text/csv",
                    key=f"dl_csv_pool_{key_prefix}",
                )
                
                raw_configs = {m.get("Name", f"Member_{i}"): m.get("Full Configuration") for i, m in enumerate(result["members"]) if m.get("Full Configuration")}
                if raw_configs:
                    with st.expander(":material/code: View Full Configuration"):
                        st.json(raw_configs)
            else:
                st.info("No members found in this pool.")
            return

        if result.get("type") in ["vip", "node", "not_found", "error"] and "target" in result:
            st.markdown(f"### :material/troubleshoot: Reverse Lookup: `{result['target']}`")
            if result["type"] in ["error", "not_found"]:
                st.warning(result.get("message") or result.get("error_message"))
                return

            if result["type"] == "vip":
                for idx, vip in enumerate(result.get("vips", []), 1):
                    st.markdown(f"### :material/router: Virtual Server: `{vip.get('vip_name')}`")
                    
                    state_str = str(vip.get('state', 'Unknown'))
                    state_icon = "✅" if state_str.lower() == "enabled" else "🛑"
                    
                    avail_str = str(vip.get('availability', 'unknown'))
                    if "available" in avail_str.lower() or "up" in avail_str.lower():
                        avail_icon = "✅"
                    elif "offline" in avail_str.lower() or "down" in avail_str.lower():
                        avail_icon = "🛑"
                    else:
                        avail_icon = "⚠️"
                        
                    col1, col2, col3 = st.columns(3)
                    col1.write(f"**Destination:** `{vip.get('destination', 'N/A')}`")
                    col2.write(f"**Admin State:** {state_icon} {state_str}")
                    col3.write(f"**Availability:** {avail_icon} {avail_str.title()}")
                    
                    st.divider()
                    
                    c1, c2 = st.columns(2)
                    c1.write(f"**Default Pool:** `{vip.get('pool') or 'None'}`")
                    c2.write(f"**Pool Monitor:** `{vip.get('monitor', 'N/A')}`")
                    
                    c3, c4 = st.columns(2)
                    irules = vip.get('iRules', [])
                    if isinstance(irules, list) and irules:
                        rule_str = " ".join([f"`{r}`" for r in irules])
                    else:
                        rule_str = "`None`"
                    c3.write(f"**Attached iRules:** {rule_str}")
                    
                    ssl_profiles = vip.get("SSL Profiles", {})
                    if ssl_profiles:
                        ssl_parts = []
                        for name, prof_data in ssl_profiles.items():
                            cert_details = []
                            for c in prof_data.get("certKeyChain", []):
                                cn = c.get("commonName")
                                exp = c.get("expirationString")
                                if cn and exp:
                                    cert_details.append(f"CN: {cn} | Exp: {exp}")
                                elif exp:
                                    cert_details.append(f"Exp: {exp}")
                            exp_text = f" *({'; '.join(cert_details)})*" if cert_details else ""
                            ssl_parts.append(f"`{name}`{exp_text}")
                        ssl_str = " ".join(ssl_parts)
                    else:
                        ssl_str = "`None`"
                    c4.write(f"**SSL Profiles:** {ssl_str}")
                    
                    st.write("")
                    if vip.get("members"):
                        st.markdown("#### :material/lan: Pool Members")
                        display_members = [{k: v for k, v in m.items() if k != "Full Configuration"} for m in vip["members"]]
                        st.dataframe(display_members, use_container_width=True)
                    else:
                        st.info("No pool members found.")
                        
                    with st.expander(":material/code: View Full Configuration"):
                        raw_data = {}
                        if vip.get("Full Configuration"):
                            raw_data["Virtual Server"] = vip["Full Configuration"]
                        if vip.get("Pool Configuration"):
                            raw_data["Pool"] = vip["Pool Configuration"]
                        if vip.get("Monitor Configuration"):
                            raw_data["Monitors"] = vip["Monitor Configuration"]
                        if vip.get("SSL Profiles"):
                            raw_data["SSL Profiles"] = vip["SSL Profiles"]
                        member_configs = {m.get("Name", f"Member_{i}"): m.get("Full Configuration") for i, m in enumerate(vip.get("members", [])) if m.get("Full Configuration")}
                        if member_configs:
                            raw_data["Pool Members"] = member_configs
                        st.json(raw_data if raw_data else {"info": "No full configuration available."})
                    st.divider()
            elif result["type"] == "node":
                if result.get("match_reason") == "monitor":
                    st.info(f"Target `{result['target']}` is attached as a Pool Monitor.")
                else:
                    st.info(f"Target `{result['target']}` is acting as a Pool Member/Node.")
                    
                for pool in result.get("pools", []):
                    st.markdown(f"### :material/lan: Pool Membership: `{pool.get('pool_name')}`")
                    if pool.get("node_status_in_pool") != "N/A (Monitor Match)":
                        st.write(f"**Node Status in Pool:** {pool.get('node_status_in_pool', 'Unknown')}")
                    if pool.get("monitor") and pool.get("monitor") != "None":
                        st.write(f"**Pool Monitor:** `{pool.get('monitor')}`")
                    if pool.get("associated_virtuals"):
                        st.markdown("#### :material/router: Affected Virtual Servers")
                        st.dataframe(pool["associated_virtuals"], use_container_width=True)
                        
                    if pool.get("Full Configuration") or pool.get("Monitor Configuration"):
                        with st.expander(":material/code: View Full Configuration"):
                            config_payload = {}
                            if pool.get("Full Configuration"):
                                config_payload["Pool"] = pool["Full Configuration"]
                            if pool.get("Monitor Configuration"):
                                config_payload["Monitors"] = pool["Monitor Configuration"]
                            st.json(config_payload)
                    st.divider()
            return
            
        if "VIP Name" in result and "Pool Members" in result:
            st.markdown(f"### :material/router: Virtual Server: `{result['VIP Name']}`")
            
            state_str = str(result.get('State', 'Unknown'))
            state_icon = "✅" if state_str.lower() == "enabled" else "🛑"
            
            avail_str = str(result.get('Availability', 'unknown'))
            if "available" in avail_str.lower() or "up" in avail_str.lower():
                avail_icon = "✅"
            elif "offline" in avail_str.lower() or "down" in avail_str.lower():
                avail_icon = "🛑"
            else:
                avail_icon = "⚠️"
                
            col1, col2, col3 = st.columns(3)
            col1.write(f"**Destination:** `{result.get('Destination', 'N/A')}`")
            col2.write(f"**Admin State:** {state_icon} {state_str}")
            col3.write(f"**Availability:** {avail_icon} {avail_str.title()}")
            
            st.divider()
            
            c1, c2 = st.columns(2)
            c1.write(f"**Default Pool:** `{result.get('Default Pool', 'None')}`")
            c2.write(f"**Pool Monitor:** `{result.get('Pool Monitor', 'N/A')}`")
            
            c3, c4 = st.columns(2)
            irules = result.get('iRules', 'None')
            if isinstance(irules, list) and irules:
                rule_str = " ".join([f"`{r}`" for r in irules])
            else:
                rule_str = "`None`"
            c3.write(f"**Attached iRules:** {rule_str}")
            
            ssl_profiles = result.get("SSL Profiles", {})
            if ssl_profiles:
                ssl_parts = []
                for name, prof_data in ssl_profiles.items():
                    cert_details = []
                    for c in prof_data.get("certKeyChain", []):
                        cn = c.get("commonName")
                        exp = c.get("expirationString")
                        if cn and exp:
                            cert_details.append(f"CN: {cn} | Exp: {exp}")
                        elif exp:
                            cert_details.append(f"Exp: {exp}")
                    exp_text = f" *({'; '.join(cert_details)})*" if cert_details else ""
                    ssl_parts.append(f"`{name}`{exp_text}")
                ssl_str = " ".join(ssl_parts)
            else:
                ssl_str = "`None`"
            c4.write(f"**SSL Profiles:** {ssl_str}")
            
            st.write("")

            if result.get("Pool Members"):
                st.markdown("#### :material/lan: Pool Members")
                display_members = [{k: v for k, v in m.items() if k != "Full Configuration"} for m in result["Pool Members"]]
                st.dataframe(display_members, use_container_width=True)
            elif result.get("Default Pool") and result.get("Default Pool") != "None":
                st.info(f"The attached pool '{result['Default Pool']}' has no active members or could not be queried.")
            else:
                st.info("No default pool is attached to this Virtual Server.")
                
            if result.get("Full Configuration") or result.get("Pool Configuration") or any(m.get("Full Configuration") for m in result.get("Pool Members", [])):
                with st.expander(":material/code: View Full Configuration"):
                    config_payload = {}
                    if result.get("Full Configuration"):
                        config_payload["Virtual Server"] = result["Full Configuration"]
                    if result.get("Pool Configuration"):
                        config_payload["Pool"] = result["Pool Configuration"]
                    if result.get("Monitor Configuration"):
                        config_payload["Monitors"] = result["Monitor Configuration"]
                    if result.get("SSL Profiles"):
                        config_payload["SSL Profiles"] = result["SSL Profiles"]
                        
                    member_configs = {m.get("Name", f"Member_{i}"): m.get("Full Configuration") for i, m in enumerate(result.get("Pool Members", [])) if m.get("Full Configuration")}
                    if member_configs:
                        config_payload["Pool Members"] = member_configs
                        
                    st.json(config_payload if config_payload else {"info": "No full configuration available."})
                
            return

        st.json(result)
        return

    st.write(result)


def render_chat_history() -> None:
    for idx, message in enumerate(st.session_state.messages):
        avatar = ":material/person:" if message["role"] == "user" else ":material/smart_toy:"
        with st.chat_message(message["role"], avatar=avatar):
            if message.get("content"):
                st.markdown(message["content"], unsafe_allow_html=True)
            if message.get("result") is not None:
                render_result(message["result"], key_prefix=f"hist_{idx}")


def main() -> None:
    apply_app_styles()
    initialize_session_state()

    render_header()
    render_status_tiles()

    with st.sidebar:
        st.info("Read-only mode is enforced before API calls.")

        with st.expander(":material/lightbulb: Example Commands"):
            st.markdown(
                """
                **Pools & Members**
                - `get pool members for <pool_name>`
                
                **Virtual Servers (VIPs)**
                - `show config of vip <ip/name>`
                
                **Nodes & Servers**
                - `check node status for <node_name>`
                
                **Reverse Lookups (IP/Hostname/Monitor)**
                - `where is <ip_hostname_or_monitor>`
                - `reverse lookup <ip_hostname_or_monitor>`
                """
            )

        st.divider()
        
        selected_template = st.selectbox(
            ":material/rocket_launch: Quick Run Template",
            options=[
                "",
                "get pool members for <pool_name>",
                "show config of vip <ip/name>",
                "check node status for <node_name>",
                "reverse lookup <ip_hostname_or_monitor>",
                "where is <ip_hostname_or_monitor>",
            ],
            format_func=lambda x: "Choose a command to run..." if x == "" else x,
        )

        trigger_run = False
        if selected_template:
            if "<" in selected_template:
                st.info(f":material/edit_note: **Template:** `{selected_template}`\n\n*Copy this text, paste it into the chat box, and replace the `< >` placeholders!*")
            else:
                trigger_run = st.button(f"Run: {selected_template}", type="primary", use_container_width=True)

        st.divider()

        if st.button(":material/delete: Clear Chat History", use_container_width=True):
            st.session_state.messages = [
                {
                    "role": "assistant",
                    "content": "Ready for F5 visibility checks. Try `show config of vip <ip/name>` or `where is 10.1.1.1`.",
                }
            ]
            st.rerun()

    chat_container = st.container()
    with chat_container:
        render_chat_history()

    user_query = st.chat_input("Try: check vip 6.6.6.6 or node 5.5.5.5 status")
    
    if trigger_run:
        user_query = selected_template

    if not user_query:
        return

    st.session_state.messages.append({"role": "user", "content": user_query})
    with chat_container:
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(user_query)

        with st.chat_message("assistant", avatar=":material/smart_toy:"):
            try:
                small_talk_response = get_small_talk_response(user_query)
                if small_talk_response:
                    st.markdown(small_talk_response)
                    st.session_state.messages.append({"role": "assistant", "content": small_talk_response})
                    return

                with st.spinner("Interpreting intent and querying BIG-IP..."):
                    action_payload, result = parse_and_execute(user_query)

                if action_payload["action"] == "unsupported":
                    assistant_content = "I could not run an F5 API request for that prompt."
                elif action_payload["action"] == "generic_chat":
                    assistant_content = action_payload.get("reasoning") or "I'm here to help with F5 tasks."
                else:
                    device_url = None
                    if isinstance(result, dict) and "device" in result:
                        device_url = result["device"]
                    elif isinstance(result, list) and result and isinstance(result[0], dict) and "Source BIG-IP" in result[0]:
                        device_url = result[0]["Source BIG-IP"]
                        
                    if device_url:
                        assistant_content = f"**Source BIG-IP:** <a href='{device_url}' target='_blank'>{device_url}</a>"
                    else:
                        assistant_content = "BIG-IP response"

                st.markdown(assistant_content, unsafe_allow_html=True)
                if result is not None:
                    render_result(result, key_prefix="current_query")

                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_content,
                        "structured": action_payload,
                        "result": result,
                    }
                )
            except (AIInterpretationError, F5ClientError) as exc:
                error_message = str(exc)
                st.error(error_message)
                st.session_state.messages.append({"role": "assistant", "content": f"Error: {error_message}"})


if __name__ == "__main__":
    main()
