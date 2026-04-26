import logging
import time
from typing import Any, Dict, List

import requests
from requests.auth import HTTPBasicAuth
import urllib3

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class F5ReverseLookupService:
    """
    Backend service for F5 BIG-IP reverse lookups.
    Supports fetching VIP or Node correlation data with in-memory caching.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
        timeout: int = 15,
        cache_ttl_seconds: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.cache_ttl = cache_ttl_seconds

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.verify = verify_ssl
        self.session.headers.update({"Content-Type": "application/json"})

        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _request(self, endpoint: str, use_cache: bool = True) -> Dict[str, Any]:
        """Wrapper for API calls with built-in caching, timeouts, and error handling."""
        if use_cache and endpoint in self.cache:
            entry = self.cache[endpoint]
            if time.time() - entry["timestamp"] < self.cache_ttl:
                logger.debug(f"Cache hit for: {endpoint}")
                return entry["data"]

        url = f"{self.base_url}{endpoint}"
        logger.info(f"API Request: GET {url}")

        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            if use_cache:
                self.cache[endpoint] = {"timestamp": time.time(), "data": data}
            return data
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout while accessing {url}")
            raise TimeoutError(f"F5 API Timeout: {str(e)}")
        except requests.exceptions.RequestException as e:
            logger.error(f"API Request failed: {e}")
            raise ConnectionError(f"F5 API Failure: {str(e)}")

    def get_virtual_by_ip(self, ip_address: str) -> List[Dict[str, Any]]:
        """Find all Virtual Servers matching a specific IP address."""
        # Using $select to prevent downloading massive full configurations
        endpoint = "/mgmt/tm/ltm/virtual?$select=name,fullPath,destination,pool,rules"
        data = self._request(endpoint)
        matched_vips = []
        
        for item in data.get("items", []):
            destination = item.get("destination", "")
            # F5 destinations format: /Common/10.1.1.1:443
            if ip_address in destination.split("/")[-1].split(":")[0]:
                matched_vips.append(item)
                
        return matched_vips

    def get_pool_members(self, pool_full_path: str) -> List[Dict[str, Any]]:
        """Fetch and format all members for a specific pool."""
        if not pool_full_path:
            return []
            
        # Sanitize partition path for REST URI (~Common~pool_name)
        safe_pool = "~" + "~".join([p for p in pool_full_path.split("/") if p])
        endpoint = f"/mgmt/tm/ltm/pool/{safe_pool}/members?$select=name,fullPath,address,state,session"
        
        try:
            data = self._request(endpoint)
            return data.get("items", [])
        except Exception as e:
            logger.warning(f"Could not fetch pool members for {pool_full_path}: {e}")
            return []

    def get_pools_by_member(self, target: str) -> List[Dict[str, Any]]:
        """Find all pools where a specific Node IP or hostname is a member."""
        # Using expandSubcollections eliminates the need to loop through hundreds of individual pool API calls
        endpoint = "/mgmt/tm/ltm/pool?expandSubcollections=true&$select=name,fullPath,membersReference"
        data = self._request(endpoint)
        matched_pools = []
        
        for pool in data.get("items", []):
            members = pool.get("membersReference", {}).get("items", [])
            for member in members:
                if target in member.get("address", "") or target in member.get("name", ""):
                    matched_pools.append(pool)
                    break
                    
        return matched_pools

    def get_virtuals_by_pool(self, pool_full_path: str) -> List[Dict[str, Any]]:
        """Find all Virtual Servers attached to a specific pool."""
        endpoint = "/mgmt/tm/ltm/virtual?$select=name,destination,pool"
        data = self._request(endpoint)
        
        return [
            item for item in data.get("items", [])
            if item.get("pool") == pool_full_path
        ]

    def reverse_lookup(self, target: str) -> Dict[str, Any]:
        """
        Main entrypoint: Decides if target is a VIP or a Node, aggregates data,
        and returns a structured JSON dictionary.
        """
        try:
            # 1. Check if the target matches any Virtual Server (VIP)
            vips = self.get_virtual_by_ip(target)
            if vips:
                vip_results = []
                for vip in vips:
                    pool_path = vip.get("pool")
                    monitor = "N/A"
                    members = []
                    
                    if pool_path:
                        safe_pool = "~" + "~".join([p for p in pool_path.split("/") if p])
                        try:
                            pool_data = self._request(f"/mgmt/tm/ltm/pool/{safe_pool}?$select=monitor")
                            monitor = pool_data.get("monitor", "None")
                        except Exception:
                            monitor = "Unknown"
                            
                        members = self.get_pool_members(pool_path)

                    vip_results.append({
                        "vip_name": vip.get("name"),
                        "destination": vip.get("destination"),
                        "pool": pool_path,
                        "monitor": monitor,
                        "iRules": vip.get("rules", []),
                        "members": [
                            {
                                "member": m.get("name"),
                                "address": m.get("address"),
                                "status": "UP" if m.get("state") == "up" else "DOWN",
                                "session": m.get("session")
                            } for m in members
                        ]
                    })
                    
                return {
                    "type": "vip",
                    "target": target,
                    "vips": vip_results
                }

            # 2. If not a VIP, check if it matches a Node/Pool Member
            pools = self.get_pools_by_member(target)
            if pools:
                pool_results = []
                for pool in pools:
                    pool_path = pool.get("fullPath")
                    vips_using_pool = self.get_virtuals_by_pool(pool_path)
                    
                    node_status = "UNKNOWN"
                    for m in pool.get("membersReference", {}).get("items", []):
                        if target in m.get("address", "") or target in m.get("name", ""):
                            node_status = "UP" if m.get("state") == "up" else "DOWN"
                            break

                    pool_results.append({
                        "pool_name": pool.get("name"),
                        "node_status_in_pool": node_status,
                        "associated_virtuals": [
                            {"name": v.get("name"), "destination": v.get("destination")}
                            for v in vips_using_pool
                        ]
                    })
                    
                return {
                    "type": "node",
                    "target": target,
                    "pools": pool_results
                }

            # 3. Target not found anywhere
            return {
                "type": "not_found",
                "target": target,
                "message": f"No Virtual Server or Node records matched '{target}'"
            }
            
        except Exception as e:
            logger.error(f"Reverse lookup failed for {target}: {str(e)}")
            return {
                "type": "error",
                "target": target,
                "error_message": str(e)
            }


def format_noc_summary(result: Dict[str, Any]) -> str:
    """Transforms the JSON payload into a human-readable summary for NOC engineers."""
    if result.get("type") in ["error", "not_found"]:
        return f"❌ Lookup Failed for {result.get('target')}: {result.get('message') or result.get('error_message')}"
        
    target = result.get("target")
    lines = [f"🔍 REVERSE LOOKUP TARGET: {target}", "=" * 45]
    
    if result.get("type") == "vip":
        for idx, vip in enumerate(result.get("vips", []), 1):
            lines.append(f"🌐 Virtual Server {idx}: {vip.get('vip_name')}")
            lines.append(f"   ├─ Destination: {vip.get('destination')}")
            lines.append(f"   ├─ iRules:      {', '.join(vip.get('iRules')) if vip.get('iRules') else 'None'}")
            lines.append(f"   ├─ Pool:        {vip.get('pool') or 'None'} (Monitor: {vip.get('monitor')})")
            lines.append(f"   └─ Pool Members ({len(vip.get('members', []))}):")
            
            for m in vip.get("members", []):
                state_icon = "✅" if m.get("status") == "UP" else "🛑"
                lines.append(f"        {state_icon} {m.get('member')} [State: {m.get('status')}, Admin: {m.get('session')}]")
            lines.append("")
            
    elif result.get("type") == "node":
        for pool in result.get("pools", []):
            status_icon = "✅" if pool.get("node_status_in_pool") == "UP" else "🛑"
            lines.append(f"🏊 Member of Pool: {pool.get('pool_name')}")
            lines.append(f"   ├─ Node Status: {status_icon} {pool.get('node_status_in_pool')}")
            lines.append(f"   └─ Affected Virtual Servers ({len(pool.get('associated_virtuals', []))}):")
            
            for v in pool.get("associated_virtuals", []):
                lines.append(f"        🔹 {v.get('name')} [{v.get('destination')}]")
            lines.append("")
            
    return "\n".join(lines)