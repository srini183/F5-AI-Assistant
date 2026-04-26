from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import urllib3
import requests


class F5ClientError(Exception):
    """Raised when the F5 client cannot complete a request."""


class F5Client:
    """Small wrapper around BIG-IP and BIG-IQ REST APIs."""

    def __init__(
        self,
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        verify_ssl: bool | None = None,
        timeout: int = 30,
    ) -> None:
        self.host = (host or os.getenv("BIGIP_HOST", os.getenv("F5_HOST", ""))).rstrip("/")
        self.username = username or os.getenv("BIGIP_USERNAME", os.getenv("F5_USERNAME", ""))
        self.password = password or os.getenv("BIGIP_PASSWORD", os.getenv("F5_PASSWORD", ""))
        self.verify_ssl = verify_ssl if verify_ssl is not None else self._env_to_bool("BIGIP_VERIFY_SSL", default=self._env_to_bool("F5_VERIFY_SSL", default=False))
        self.timeout = timeout

        if self._is_placeholder_host(self.host):
            self.host = ""

        if not self.host:
            raise F5ClientError("Missing real BIGIP_HOST or F5_HOST environment variable.")
        if not self.username or not self.password:
            raise F5ClientError("Missing BIGIP_USERNAME/BIGIP_PASSWORD or F5_USERNAME/F5_PASSWORD environment variables.")

        self.session = requests.Session()
        self.session.verify = self.verify_ssl
        self.session.headers.update({"Content-Type": "application/json"})

        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._authenticate()

    def _authenticate(self) -> None:
        auth_url = self._build_url("/mgmt/shared/authn/login")
        payload = {"username": self.username, "password": self.password, "loginProviderName": "tmos"}

        try:
            response = self.session.post(auth_url, json=payload, timeout=self.timeout)
            if response.status_code == 401:
                # Fallback for Active Directory / LDAP remote auth users
                payload.pop("loginProviderName")
                response = self.session.post(auth_url, json=payload, timeout=self.timeout)
                if response.status_code == 401:
                    raise F5ClientError(f"Authentication failed (401) for {self.host}. Please verify BIGIP_USERNAME and BIGIP_PASSWORD.")
            response.raise_for_status()
            token = response.json()["token"]["token"]
            self.session.headers.update({"X-F5-Auth-Token": token})
        except F5ClientError:
            raise
        except requests.RequestException:
            self.session.auth = (self.username, self.password)

    @staticmethod
    def _env_to_bool(name: str, default: bool = False) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _is_placeholder_host(host: str | None) -> bool:
        if not host:
            return True
        normalized = host.strip().lower()
        return "your-bigip.example.com" in normalized

    def _build_url(self, path: str) -> str:
        return f"{self.host}{path}"

    def _bigip_pool_reference(self, pool_name: str) -> str:
        cleaned_name = self._clean_object_name(pool_name)
        if not cleaned_name:
            raise F5ClientError("Pool name is required for get_pool_members.")

        path_parts = [part for part in cleaned_name.split("/") if part]
        if len(path_parts) == 1:
            # Auto-discover the partition if only the pool name is provided
            try:
                data = self._request("/mgmt/tm/ltm/pool?$select=name,fullPath")
                for item in data.get("items", []):
                    if self._clean_object_name(item.get("name", "")).lower() == path_parts[0].lower():
                        resolved_path = item.get("fullPath", "")
                        if resolved_path:
                            path_parts = [p for p in resolved_path.split("/") if p]
                        break
                else:
                    path_parts.insert(0, "Common")
            except Exception:
                path_parts.insert(0, "Common")

        encoded_parts = [quote(part, safe="") for part in path_parts]
        return f"~{'~'.join(encoded_parts)}"

    @staticmethod
    def _clean_object_name(name: str) -> str:
        return name.strip().strip("`'\"").strip("/")

    @staticmethod
    def _object_matches(requested_name: str, item: dict[str, Any]) -> bool:
        requested = F5Client._clean_object_name(requested_name).lower()
        if not requested:
            return False
            
        candidates = [
            F5Client._clean_object_name(str(item.get("name") or "")).lower(),
            F5Client._clean_object_name(str(item.get("fullPath") or "")).lower(),
            F5Client._clean_object_name(str(item.get("id") or "")).lower(),
            F5Client._clean_object_name(str(item.get("destination") or "")).lower(),
            F5Client._clean_object_name(str(item.get("destinationAddress") or "")).lower(),
            F5Client._clean_object_name(str(item.get("address") or "")).lower(),
        ]
        for candidate in candidates:
            if candidate and requested in candidate:
                return True
        return False

    def _request(self, path: str) -> dict[str, Any]:
        url = self._build_url(path)
        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code == 401:
                raise F5ClientError(f"Authentication failed (401) for {self.host}. Please verify your credentials.")
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise F5ClientError(f"F5 API request failed for {url}: {exc}") from exc
        except ValueError as exc:
            raise F5ClientError(f"F5 API returned a non-JSON response for {url}.") from exc

        if not isinstance(data, dict):
            raise F5ClientError(f"F5 API returned an unexpected response shape for {url}.")
        return data

    @staticmethod
    def _normalize_partition(value: str | None) -> str:
        if not value:
            return "Common"
        return value.strip().strip("/")

    @staticmethod
    def _normalize_member(member: dict[str, Any]) -> dict[str, Any]:
        state = str(member.get("state", "unknown")).lower()
        state_icon = "✅" if state == "up" else ("🛑" if state in ["down", "offline"] else "⚠️")
        
        avail = str(member.get("availabilityState", "unknown")).lower()
        if "available" in avail or "up" in avail:
            avail_icon = "✅"
        elif "offline" in avail or "down" in avail:
            avail_icon = "🛑"
        else:
            avail_icon = "⚠️"
            
        return {
            "Name": member.get("name"),
            "Address": member.get("address"),
            "State": f"{state_icon} {state.title()}",
            "Session": str(member.get("session", "unknown")).title(),
            "Availability": f"{avail_icon} {avail.title()}",
            "Full Configuration": member,
        }
        
    def _get_monitor_details(self, monitor_rule: str) -> dict[str, Any]:
        if not monitor_rule or monitor_rule == "None" or monitor_rule == "Unknown":
            return {}
            
        import re
        # Extract paths (handles complex F5 rules like: "min 1 of { /Common/tcp /Common/http }")
        paths = re.findall(r'(?:/[\w.\-:]+)+', monitor_rule)
        if not paths:
            return {}
            
        monitor_configs = {}
        types_to_try = [
            "http", "https", "tcp", "udp", "gateway-icmp", "icmp", "tcp-half-open", 
            "dns", "inband", "external", "sip", "smtp", "ftp", "pop3", "imap", 
            "ldap", "radius", "mysql", "postgresql", "mssql", "oracle", "snmp", 
            "wmi", "diameter", "virtual-server", "none"
        ]
        
        for path in set(paths):
            path_parts = [p for p in path.split("/") if p]
            encoded_parts = [quote(part, safe="") for part in path_parts]
            safe_path = f"~{'~'.join(encoded_parts)}"
            found = False
            for m_type in types_to_try:
                try:
                    data = self._request(f"/mgmt/tm/ltm/monitor/{m_type}/{safe_path}")
                    monitor_configs[path] = data
                    found = True
                    break
                except F5ClientError:
                    pass
            if not found:
                monitor_configs[path] = {"info": f"Monitor not found. It may be deleted, unsupported, or your F5 user account lacks read permissions to the partition."}
                
        return monitor_configs

    def _enrich_cert_key_chain(self, chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in chain:
            cert_path = item.get("cert", "")
            if cert_path and cert_path.lower() != "none":
                try:
                    enc_cert = f"~{'~'.join([quote(part, safe='') for part in cert_path.strip('/').split('/') if part])}"
                    cert_data = self._request(f"/mgmt/tm/sys/file/ssl-cert/{enc_cert}")
                    item["expirationString"] = cert_data.get("expirationString", "Unknown")
                    subject = cert_data.get("subject", "")
                    cn = ""
                    for part in subject.split(","):
                        if part.strip().startswith("CN="):
                            cn = part.strip()[3:]
                            break
                    item["commonName"] = cn if cn else subject
                except Exception:
                    pass
        return chain

    def _get_ssl_profiles(self, vip_encoded_path: str) -> dict[str, Any]:
        ssl_profiles = {}
        try:
            profiles_data = self._request(f"/mgmt/tm/ltm/virtual/{vip_encoded_path}/profiles")
            for prof in profiles_data.get("items", []):
                p_name = prof.get("name")
                p_full_path = prof.get("fullPath", "")
                if not p_full_path:
                    continue
                    
                encoded_p = f"~{'~'.join([quote(part, safe='') for part in p_full_path.strip('/').split('/') if part])}"
                
                # Try client-ssl
                try:
                    cssl = self._request(f"/mgmt/tm/ltm/profile/client-ssl/{encoded_p}")
                    chain = self._enrich_cert_key_chain(cssl.get("certKeyChain", []))
                    ssl_profiles[p_name] = {
                        "type": "client-ssl",
                        "context": prof.get("context", "clientside"),
                        "certKeyChain": chain,
                        "ciphers": cssl.get("ciphers", ""),
                        "full_profile": cssl
                    }
                    continue
                except F5ClientError:
                    pass
                    
                # Try server-ssl
                try:
                    sssl = self._request(f"/mgmt/tm/ltm/profile/server-ssl/{encoded_p}")
                    chain = self._enrich_cert_key_chain(sssl.get("certKeyChain", []))
                    ssl_profiles[p_name] = {
                        "type": "server-ssl",
                        "context": prof.get("context", "serverside"),
                        "certKeyChain": chain,
                        "ciphers": sssl.get("ciphers", ""),
                        "full_profile": sssl
                    }
                except F5ClientError:
                    pass
        except Exception:
            pass
        return ssl_profiles

    @classmethod
    def _normalize_bigip_pool(cls, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("fullPath") or item.get("name"),
            "name": item.get("name"),
            "fullPath": item.get("fullPath"),
            "loadBalancingMode": item.get("loadBalancingMode"),
            "monitor": item.get("monitor"),
            "source": "bigip",
        }

    @classmethod
    def _normalize_bigiq_pool(cls, item: dict[str, Any]) -> dict[str, Any]:
        partition = cls._normalize_partition(item.get("partition"))
        name = item.get("name")
        full_path = f"/{partition}/{name}" if name else None
        return {
            "id": item.get("id"),
            "name": name,
            "fullPath": full_path,
            "loadBalancingMode": item.get("loadBalancingMode"),
            "monitor": item.get("monitor"),
            "source": "bigiq",
        }

    @staticmethod
    def _normalize_virtual(item: dict[str, Any], source: str) -> dict[str, Any]:
        is_enabled = item.get("enabled")
        if is_enabled is None:
            # BIG-IP typically omits 'enabled' and just sets 'disabled': True when disabled
            is_enabled = not item.get("disabled", False)
            
        availability = item.get("availabilityState") or ("unknown (needs stats)" if source == "bigip" else "unknown")

        pool = item.get("pool", "")
        if pool and pool.startswith("/Common/"):
            pool = pool.replace("/Common/", "")

        return {
            "name": item.get("name"),
            "fullPath": item.get("fullPath"),
            "destination": item.get("destination") or item.get("destinationAddress"),
            "pool": pool,
            "enabled": is_enabled,
            "availability": availability,
        }

    @staticmethod
    def _normalize_node(item: dict[str, Any]) -> dict[str, Any]:
        state = str(item.get("state", "unknown")).lower()
        state_icon = "✅" if state == "up" else ("🛑" if state in ["down", "offline"] else "⚠️")
        return {
            "Name": item.get("name"),
            "Address": item.get("address"),
            "State": f"{state_icon} {state.title()}",
            "Session": str(item.get("session", "unknown")).title(),
            "Full Configuration": item,
        }

    def get_pool_members(self, pool_name: str) -> list[dict[str, Any]]:
        pool_reference = self._bigip_pool_reference(pool_name)
        try:
            data = self._request(f"/mgmt/tm/ltm/pool/{pool_reference}/members")
        except F5ClientError as exc:
            if "404" in str(exc):
                raise F5ClientError(f"Pool '{pool_name}' was not found on BIG-IP.") from exc
            raise
            
        members = data.get("items", [])
        
        try:
            stats_data = self._request(f"/mgmt/tm/ltm/pool/{pool_reference}/members/stats")
            stats_entries = stats_data.get("entries", {})
            for member in members:
                full_path = member.get("fullPath", "")
                if not full_path:
                    continue
                
                path_parts = [part for part in full_path.strip("/").split("/") if part]
                encoded_parts = [quote(part, safe="") for part in path_parts]
                encoded_path = f"~{'~'.join(encoded_parts)}"
                search_str = f"{encoded_path}/stats"
                
                for stats_key, stats_body in stats_entries.items():
                    if search_str in stats_key:
                        nested = stats_body.get("nestedStats", {}).get("entries", {})
                        state = nested.get("status.availabilityState", {}).get("description")
                        if state:
                            member["availabilityState"] = state
                        break
        except F5ClientError:
            pass
            
        return [self._normalize_member(member) for member in members]

    def _get_vip_status(self, virtual_server_name: str | None = None) -> list[dict[str, Any]]:
        # Using $select massively speeds up the query and avoids timeouts
        data = self._request("/mgmt/tm/ltm/virtual?$select=name,fullPath,destination,destinationAddress,pool,enabled,disabled")
        items = data.get("items", [])
        if virtual_server_name:
            items = [item for item in items if self._object_matches(virtual_server_name, item)]
            if not items:
                raise F5ClientError(f"VIP or virtual server '{virtual_server_name}' was not found on BIG-IP.")

        for item in items:
            full_path = item.get("fullPath", "")
            if not full_path:
                continue
            
            path_parts = [part for part in full_path.strip("/").split("/") if part]
            encoded_parts = [quote(part, safe="") for part in path_parts]
            encoded_path = f"~{'~'.join(encoded_parts)}"
            
            try:
                stats_data = self._request(f"/mgmt/tm/ltm/virtual/{encoded_path}/stats")
                for stats_body in stats_data.get("entries", {}).values():
                    nested = stats_body.get("nestedStats", {}).get("entries", {})
                    state = nested.get("status.availabilityState", {}).get("description")
                    if state:
                        item["availabilityState"] = state
                        break
            except F5ClientError:
                pass

        return [self._normalize_virtual(item, "bigip") for item in items]

    def get_nodes(self, node_name: str) -> list[dict[str, Any]]:
        if not node_name:
            raise F5ClientError("Node name or IP is required.")
        data = self._request("/mgmt/tm/ltm/node")
        items = data.get("items", [])
        items = [item for item in items if self._object_matches(node_name, item)]
        if not items:
            raise F5ClientError(f"Node '{node_name}' was not found on BIG-IP.")
        return [self._normalize_node(item) for item in items]

    def get_vip_details(self, virtual_server_name: str) -> dict[str, Any]:
        if not virtual_server_name:
            raise F5ClientError("Virtual server name is required for configuration details.")
            
        vips = self._get_vip_status(virtual_server_name)
        if not vips:
            raise F5ClientError(f"VIP or virtual server '{virtual_server_name}' was not found.")
        vip = vips[0]
        
        full_path = vip.get("fullPath")
        if full_path:
            path_parts = [part for part in full_path.strip("/").split("/") if part]
            encoded_parts = [quote(part, safe="") for part in path_parts]
            encoded_path = f"~{'~'.join(encoded_parts)}"
            try:
                raw_vip = self._request(f"/mgmt/tm/ltm/virtual/{encoded_path}")
            except F5ClientError:
                raw_vip = {}
        else:
            raw_vip = {}
        
        pool_path = raw_vip.get("pool", "")
        pool_name = pool_path.strip() if pool_path else None
        
        rules = raw_vip.get("rules", [])
        if isinstance(rules, list):
            rules = [r.split("/")[-1] for r in rules]
            
        display_pool = pool_name or "None"
        if display_pool.startswith("/Common/"):
            display_pool = display_pool.replace("/Common/", "")
            
        ssl_configs = self._get_ssl_profiles(encoded_path) if full_path else {}
            
        details = {
            "VIP Name": vip.get("name"),
            "Destination": vip.get("destination"),
            "State": "Enabled" if vip.get("enabled") else "Disabled",
            "Availability": vip.get("availability"),
            "iRules": rules or "None",
            "Default Pool": display_pool,
            "Pool Monitor": "N/A",
            "SSL Profiles": ssl_configs,
            "Pool Members": [],
            "Full Configuration": raw_vip
        }
        
        if pool_name:
            try:
                pool_data = self._request(f"/mgmt/tm/ltm/pool/{self._bigip_pool_reference(pool_name)}")
                details["Pool Monitor"] = pool_data.get("monitor", "None")
                details["Pool Configuration"] = pool_data
                details["Monitor Configuration"] = self._get_monitor_details(details["Pool Monitor"])
            except Exception:
                details["Pool Monitor"] = "Unknown (Error fetching)"
                    
            try:
                details["Pool Members"] = self.get_pool_members(pool_name)
            except Exception:
                pass  # Safely ignore if user lacks permission to view pool members
                
        return details

    def reverse_lookup(self, target: str) -> dict[str, Any]:
        try:
            # 1. Check if the target matches any Virtual Server (VIP)
            vip_endpoint = "/mgmt/tm/ltm/virtual?$select=name,fullPath,destination,pool,rules,enabled,disabled"
            vip_data = self._request(vip_endpoint)
            matched_vips = []
            
            for item in vip_data.get("items", []):
                destination = item.get("destination", "")
                if target in destination.split("/")[-1].split(":")[0]:
                    matched_vips.append(item)
                    
            if matched_vips:
                vip_results = []
                for vip in matched_vips:
                    pool_path = vip.get("pool")
                    monitor = "N/A"
                    members = []
                    pool_data_full = {}
                    ssl_configs = {}
                    
                    is_enabled = vip.get("enabled")
                    if is_enabled is None:
                        is_enabled = not vip.get("disabled", False)
                        
                    availability = "unknown"
                    full_path = vip.get("fullPath", "")
                    if full_path:
                        path_parts = [part for part in full_path.strip("/").split("/") if part]
                        encoded_parts = [quote(part, safe="") for part in path_parts]
                        encoded_path = f"~{'~'.join(encoded_parts)}"
                        try:
                            stats_data = self._request(f"/mgmt/tm/ltm/virtual/{encoded_path}/stats")
                            for stats_body in stats_data.get("entries", {}).values():
                                nested = stats_body.get("nestedStats", {}).get("entries", {})
                                state = nested.get("status.availabilityState", {}).get("description")
                                if state:
                                    availability = state
                                    break
                        except F5ClientError:
                            pass
                            
                        ssl_configs = self._get_ssl_profiles(encoded_path)
                    
                    if pool_path:
                        safe_pool = "~" + "~".join([p for p in pool_path.split("/") if p])
                        try:
                            pool_data_full = self._request(f"/mgmt/tm/ltm/pool/{safe_pool}")
                            monitor = pool_data_full.get("monitor", "None")
                        except Exception:
                            monitor = "Unknown"
                            
                        try:
                            mem_data = self._request(f"/mgmt/tm/ltm/pool/{safe_pool}/members?$select=name,fullPath,address,state,session")
                            members = mem_data.get("items", [])
                        except Exception:
                            pass
                            
                    monitor_config = self._get_monitor_details(monitor)

                    vip_results.append({
                        "vip_name": vip.get("name"),
                        "destination": vip.get("destination"),
                        "state": "Enabled" if is_enabled else "Disabled",
                        "availability": availability,
                        "pool": pool_path,
                        "monitor": monitor,
                        "iRules": vip.get("rules", []),
                        "SSL Profiles": ssl_configs,
                        "members": [self._normalize_member(m) for m in members],
                        "Full Configuration": vip,
                        "Pool Configuration": pool_data_full,
                        "Monitor Configuration": monitor_config
                    })
                    
                return {"type": "vip", "target": target, "device": self.host, "vips": vip_results}

            # 2. If not a VIP, check if it matches a Node/Pool Member
            pool_endpoint = "/mgmt/tm/ltm/pool?expandSubcollections=true&$select=name,fullPath,membersReference,monitor"
            pool_data = self._request(pool_endpoint)
            matched_pools = []
            
            for pool in pool_data.get("items", []):
                members = pool.get("membersReference", {}).get("items", [])
                matched = False
                for member in members:
                    if target in member.get("address", "") or target in member.get("name", ""):
                        matched_pools.append({"pool": pool, "reason": "node"})
                        matched = True
                        break
                        
                if not matched:
                    monitor_str = pool.get("monitor", "")
                    if monitor_str and monitor_str != "None" and target in monitor_str:
                        matched_pools.append({"pool": pool, "reason": "monitor"})

            if matched_pools:
                pool_results = []
                all_vips = self._request("/mgmt/tm/ltm/virtual?$select=name,destination,pool").get("items", [])
                
                is_monitor_only = all(m["reason"] == "monitor" for m in matched_pools)
                
                for match in matched_pools:
                    pool = match["pool"]
                    pool_path = pool.get("fullPath")
                    vips_using_pool = [v for v in all_vips if v.get("pool") == pool_path]
                    
                    node_status = "UNKNOWN"
                    if match["reason"] == "node":
                        for m in pool.get("membersReference", {}).get("items", []):
                            if target in m.get("address", "") or target in m.get("name", ""):
                                normalized_m = self._normalize_member(m)
                                node_status = normalized_m["State"]
                                break
                    else:
                        node_status = "N/A (Monitor Match)"
                            
                    try:
                        safe_pool = "~" + "~".join([p for p in pool_path.split("/") if p])
                        full_pool_data = self._request(f"/mgmt/tm/ltm/pool/{safe_pool}")
                        monitor = full_pool_data.get("monitor", "None")
                    except Exception:
                        full_pool_data = pool
                        monitor = pool.get("monitor", "None")
                        
                    monitor_config = self._get_monitor_details(monitor)

                    pool_results.append({
                        "pool_name": pool.get("name"),
                        "node_status_in_pool": node_status,
                        "monitor": monitor,
                        "associated_virtuals": [{"Name": v.get("name"), "Destination": v.get("destination")} for v in vips_using_pool],
                        "Full Configuration": full_pool_data,
                        "Monitor Configuration": monitor_config
                    })
                return {"type": "node", "match_reason": "monitor" if is_monitor_only else "node", "target": target, "device": self.host, "pools": pool_results}

            return {"type": "not_found", "target": target, "device": self.host, "message": f"No Virtual Server or Node records matched '{target}'"}
        except Exception as e:
            return {"type": "error", "target": target, "device": self.host, "error_message": str(e)}
