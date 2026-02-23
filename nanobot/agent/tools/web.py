"""Web tools: web_search and web_fetch."""

import html
import ipaddress
import json
import os
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from nanobot.agent.tools.base import Tool
from nanobot.logging import get_logger

audit_log = get_logger("nanobot.audit")

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB response body limit
DNS_TIMEOUT = 5  # seconds

# Private / reserved IP ranges that must never be reached
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _is_private_ip(addr: str) -> bool:
    """Return True if *addr* belongs to a private/reserved network."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _BLOCKED_NETWORKS)


def _resolve_host(hostname: str) -> list[str]:
    """Resolve *hostname* to IP addresses with a timeout."""
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return list({info[4][0] for info in infos})
    except socket.gaierror:
        return []


def _check_url_ssrf(url: str) -> tuple[bool, str]:
    """Validate that *url* does not target a private/reserved IP.

    Resolves the hostname via DNS and checks every returned address.
    Returns ``(ok, error_message)``.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return False, "Missing hostname"

    # Direct IP literal check
    if _is_private_ip(hostname):
        return False, f"Access to private/reserved IP {hostname} is blocked"

    # DNS resolution check
    addrs = _resolve_host(hostname)
    if not addrs:
        return False, f"Could not resolve hostname: {hostname}"
    for addr in addrs:
        if _is_private_ip(addr):
            return False, f"Hostname {hostname} resolves to private/reserved IP {addr}"

    return True, ""


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain, not targeting private IPs."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        # SSRF check
        ok, err = _check_url_ssrf(url)
        if not ok:
            return False, err
        return True, ""
    except Exception as e:
        return False, str(e)


class WebSearchTool(Tool):
    """Search the web using Brave Search API."""
    
    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10}
        },
        "required": ["query"]
    }
    
    def __init__(self, api_key: str | None = None, max_results: int = 5):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        self.max_results = max_results
    
    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        if not self.api_key:
            return "Error: BRAVE_API_KEY not configured"
        
        try:
            n = min(max(count or self.max_results, 1), 10)
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                    timeout=10.0
                )
                r.raise_for_status()
            
            results = r.json().get("web", {}).get("results", [])
            if not results:
                return f"No results for: {query}"
            
            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if desc := item.get("description"):
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"


class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability."""
    
    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100}
        },
        "required": ["url"]
    }
    
    def __init__(self, max_chars: int = 50000):
        self.max_chars = max_chars
    
    async def execute(self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:
        from readability import Document

        max_chars = maxChars or self.max_chars

        # Validate URL before fetching (includes SSRF check)
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            audit_log.warning("web_fetch_blocked", url=url, reason=error_msg)
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        try:
            # Manual redirect loop so we can re-validate each hop
            current_url = url
            r = None
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=30.0,
            ) as client:
                for _hop in range(MAX_REDIRECTS + 1):
                    r = await client.get(current_url, headers={"User-Agent": USER_AGENT})
                    if r.is_redirect:
                        location = r.headers.get("location", "")
                        if not location:
                            break
                        # Resolve relative redirects
                        next_url = str(r.url.join(location))
                        ok, err = _check_url_ssrf(next_url)
                        if not ok:
                            audit_log.warning("web_fetch_redirect_blocked", url=url, redirect_url=next_url, reason=err)
                            return json.dumps({"error": f"Redirect blocked (SSRF): {err}", "url": url, "redirect_url": next_url}, ensure_ascii=False)
                        current_url = next_url
                        continue
                    break

                r.raise_for_status()

            # Enforce response body size limit
            body_len = len(r.content)
            if body_len > MAX_RESPONSE_BYTES:
                return json.dumps({"error": f"Response too large ({body_len} bytes, limit {MAX_RESPONSE_BYTES})", "url": url}, ensure_ascii=False)

            ctype = r.headers.get("content-type", "")

            # JSON
            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2, ensure_ascii=False), "json"
            # HTML
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = self._to_markdown(doc.summary()) if extractMode == "markdown" else _strip_tags(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            audit_log.info("web_fetch", url=url, final_url=str(r.url), status=r.status_code)
            return json.dumps({"url": url, "finalUrl": str(r.url), "status": r.status_code,
                              "extractor": extractor, "truncated": truncated, "length": len(text), "text": text}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)
    
    def _to_markdown(self, html: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))
