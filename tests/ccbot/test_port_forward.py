"""Tests for port forwarding URL parsing."""

from ccbot.port_forward import _LOCALHOST_RUN_URL_RE


def test_localhost_run_regex_ignores_admin_url() -> None:
    assert _LOCALHOST_RUN_URL_RE.search("https://admin.localhost.run/") is None


def test_localhost_run_regex_matches_random_subdomain() -> None:
    m = _LOCALHOST_RUN_URL_RE.search("tunnel: https://a1b2c3d4.localhost.run")
    assert m is not None
    assert m.group(0) == "https://a1b2c3d4.localhost.run"
