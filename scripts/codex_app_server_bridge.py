#!/usr/bin/env python3
"""Compatibility wrapper for app-server bridge.

Prefer using: `ccbot --app ...`
"""

from ccbot.app_server_bridge import app_bridge_main


if __name__ == "__main__":
    raise SystemExit(app_bridge_main())
