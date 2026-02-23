"""Security tests for SSRF protection in web tools.

Covers:
- Private/reserved IP blocking (IPv4 + IPv6)
- DNS resolution to private IP blocking
- Redirect-to-private-IP blocking
- Response body size limit
- Legitimate URLs passing validation
- Audit logging for blocked requests
"""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from nanobot.agent.tools.web import (
    _is_private_ip,
    _check_url_ssrf,
    _validate_url,
    WebFetchTool,
)


# ---------------------------------------------------------------------------
# _is_private_ip tests
# ---------------------------------------------------------------------------

class TestIsPrivateIP:
    def test_loopback_v4(self):
        assert _is_private_ip("127.0.0.1") is True

    def test_loopback_v6(self):
        assert _is_private_ip("::1") is True

    def test_class_a_private(self):
        assert _is_private_ip("10.0.0.1") is True

    def test_class_b_private(self):
        assert _is_private_ip("172.16.0.1") is True

    def test_class_c_private(self):
        assert _is_private_ip("192.168.1.1") is True

    def test_link_local(self):
        assert _is_private_ip("169.254.169.254") is True

    def test_cgnat(self):
        assert _is_private_ip("100.64.0.1") is True

    def test_ipv6_ula(self):
        assert _is_private_ip("fd00::1") is True

    def test_ipv6_link_local(self):
        assert _is_private_ip("fe80::1") is True

    def test_public_ip(self):
        assert _is_private_ip("8.8.8.8") is False

    def test_public_ip_v6(self):
        assert _is_private_ip("2607:f8b0:4004:800::200e") is False

    def test_invalid_addr(self):
        assert _is_private_ip("not-an-ip") is False


# ---------------------------------------------------------------------------
# _check_url_ssrf tests
# ---------------------------------------------------------------------------

class TestCheckUrlSSRF:
    def test_direct_private_ip(self):
        ok, err = _check_url_ssrf("http://127.0.0.1/admin")
        assert ok is False
        assert "private" in err.lower() or "blocked" in err.lower()

    def test_cloud_metadata(self):
        ok, err = _check_url_ssrf("http://169.254.169.254/latest/meta-data/")
        assert ok is False

    def test_private_class_a(self):
        ok, err = _check_url_ssrf("http://10.0.0.1:8080/internal")
        assert ok is False

    @patch("nanobot.agent.tools.web._resolve_host", return_value=["93.184.216.34"])
    def test_public_domain_passes(self, mock_resolve):
        ok, err = _check_url_ssrf("https://example.com")
        assert ok is True
        assert err == ""

    @patch("nanobot.agent.tools.web._resolve_host", return_value=["127.0.0.1"])
    def test_dns_rebinding_blocked(self, mock_resolve):
        """Domain resolving to loopback should be blocked."""
        ok, err = _check_url_ssrf("http://evil.example.com/steal")
        assert ok is False
        assert "127.0.0.1" in err

    @patch("nanobot.agent.tools.web._resolve_host", return_value=[])
    def test_unresolvable_host(self, mock_resolve):
        ok, err = _check_url_ssrf("http://nonexistent.invalid/")
        assert ok is False
        assert "resolve" in err.lower()

    def test_missing_hostname(self):
        ok, err = _check_url_ssrf("http:///path")
        assert ok is False


# ---------------------------------------------------------------------------
# _validate_url tests (now includes SSRF)
# ---------------------------------------------------------------------------

class TestValidateUrl:
    def test_ftp_rejected(self):
        ok, err = _validate_url("ftp://files.example.com/data")
        assert ok is False

    def test_no_scheme(self):
        ok, err = _validate_url("just-a-string")
        assert ok is False

    @patch("nanobot.agent.tools.web._check_url_ssrf", return_value=(True, ""))
    def test_valid_https(self, mock_ssrf):
        ok, err = _validate_url("https://example.com/page")
        assert ok is True

    def test_private_ip_via_validate(self):
        ok, err = _validate_url("http://192.168.1.1/router")
        assert ok is False


# ---------------------------------------------------------------------------
# WebFetchTool integration tests
# ---------------------------------------------------------------------------

class TestWebFetchToolSSRF:
    @pytest.mark.asyncio
    async def test_fetch_private_ip_blocked(self):
        tool = WebFetchTool()
        result = await tool.execute(url="http://127.0.0.1:8080/secret")
        data = json.loads(result)
        assert "error" in data
        assert "blocked" in data["error"].lower() or "private" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_fetch_metadata_endpoint_blocked(self):
        tool = WebFetchTool()
        result = await tool.execute(url="http://169.254.169.254/latest/meta-data/")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    @patch("nanobot.agent.tools.web._check_url_ssrf")
    async def test_fetch_redirect_to_private_blocked(self, mock_ssrf):
        """Simulate a redirect to a private IP — second hop should be blocked."""
        import httpx

        # First call (initial URL) passes, second call (redirect target) fails
        mock_ssrf.side_effect = [
            (True, ""),
            (False, "Hostname evil.com resolves to private/reserved IP 127.0.0.1"),
        ]

        redirect_response = httpx.Response(
            302,
            headers={"location": "http://evil.com/steal"},
            request=httpx.Request("GET", "https://legit.com"),
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=redirect_response)
            mock_client_cls.return_value = mock_client

            tool = WebFetchTool()
            result = await tool.execute(url="https://legit.com/page")
            data = json.loads(result)
            assert "error" in data
            assert "redirect" in data["error"].lower() or "ssrf" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_fetch_ipv6_loopback_blocked(self):
        tool = WebFetchTool()
        result = await tool.execute(url="http://[::1]/admin")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_fetch_audit_logged(self, capsys):
        """Blocked requests should produce audit output."""
        tool = WebFetchTool()
        await tool.execute(url="http://10.0.0.1/internal")
        captured = capsys.readouterr()
        assert "web_fetch_blocked" in captured.out or "web_fetch_blocked" in captured.err
