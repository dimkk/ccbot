"""Tests for CLI helpers in main.py."""

import pytest

from ccbot.main import _looks_like_ccbot_process, _parse_forward_ports


def test_parse_forward_ports_empty() -> None:
    assert _parse_forward_ports([]) == []


def test_parse_forward_ports_multiple() -> None:
    ports = _parse_forward_ports(["--forward", "3000,5173", "--forward", "8080"])
    assert ports == [3000, 5173, 8080]


def test_parse_forward_ports_invalid() -> None:
    with pytest.raises(SystemExit):
        _parse_forward_ports(["--forward", "abc"])


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        ("uv run ccbot", True),
        ("/home/u/.local/bin/ccbot", True),
        ("/repo/.venv/bin/python /repo/.venv/bin/ccbot", True),
        ("python -m ccbot", True),
        ("python3 -m ccbot", True),
        ("uv run something-else", False),
        ("python app.py", False),
    ],
)
def test_looks_like_ccbot_process(cmdline: str, expected: bool) -> None:
    assert _looks_like_ccbot_process(cmdline) is expected
