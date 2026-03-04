"""Application entry point — CLI dispatcher and bot bootstrap.

Handles two execution modes:
  1. `ccbot hook` — delegates to hook.hook_main() for Claude Code hook processing.
  2. `ccbot codex-map` — updates session_map.json for Codex rollout sessions.
  3. Default — configures logging, initializes tmux session, and starts the
     Telegram bot polling loop via bot.create_bot().
"""

import asyncio
import argparse
import logging
import os
import signal
import subprocess
import sys
import time


def _parse_forward_ports(args: list[str]) -> list[int]:
    parser = argparse.ArgumentParser(
        prog="ccbot",
        description="Telegram monitor for AI CLI sessions",
    )
    parser.add_argument(
        "--forward",
        action="append",
        default=[],
        metavar="PORT[,PORT...]",
        help="Forward local port(s) to public URL and announce in Telegram",
    )
    parsed = parser.parse_args(args)

    ports: list[int] = []
    for group in parsed.forward:
        for token in group.split(","):
            part = token.strip()
            if not part:
                continue
            try:
                port = int(part)
            except ValueError as e:
                raise SystemExit(f"Invalid --forward value: {part}") from e
            if port < 1 or port > 65535:
                raise SystemExit(f"Invalid --forward port: {port}")
            ports.append(port)
    return ports


def _looks_like_ccbot_process(cmdline: str) -> bool:
    """Return True when a process cmdline appears to be a ccbot runner."""
    text = cmdline.strip()
    if not text:
        return False
    parts = text.split()
    if not parts:
        return False

    # Direct launcher script (venv/local/bin/ccbot or plain ccbot)
    if parts[0] == "ccbot" or parts[0].endswith("/ccbot"):
        return True
    if any(token.endswith("/bin/ccbot") for token in parts):
        return True

    # uv run ccbot
    for i in range(len(parts) - 2):
        if parts[i] == "uv" and parts[i + 1] == "run" and parts[i + 2] == "ccbot":
            return True

    # python -m ccbot style
    for i in range(len(parts) - 2):
        if (
            parts[i].startswith("python")
            and parts[i + 1] == "-m"
            and parts[i + 2] == "ccbot"
        ):
            return True

    return False


def _read_ppid(pid: int) -> int:
    """Read parent pid from /proc/<pid>/stat; returns 0 on failure."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            fields = f.read().split()
        if len(fields) >= 4:
            return int(fields[3])
    except (OSError, ValueError):
        pass
    return 0


def _protected_pid_set() -> set[int]:
    """Current pid and all ancestors (must never be terminated)."""
    protected: set[int] = set()
    pid = os.getpid()
    while pid > 1 and pid not in protected:
        protected.add(pid)
        pid = _read_ppid(pid)
    if pid == 1:
        protected.add(1)
    return protected


def _list_process_table() -> list[tuple[int, str]]:
    """Return [(pid, cmdline)] from `ps` output."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return []

    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmdline = parts[1] if len(parts) > 1 else ""
        rows.append((pid, cmdline))
    return rows


def _terminate_other_ccbot_instances(logger: logging.Logger) -> None:
    """Terminate older ccbot instances to avoid Telegram getUpdates conflicts."""
    protected = _protected_pid_set()
    targets: list[int] = []
    for pid, cmdline in _list_process_table():
        if pid in protected:
            continue
        if _looks_like_ccbot_process(cmdline):
            targets.append(pid)

    if not targets:
        return

    # First try graceful stop.
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            logger.warning("No permission to terminate stale ccbot pid=%d", pid)
        except OSError as e:
            logger.warning("Failed to terminate stale ccbot pid=%d: %s", pid, e)

    deadline = time.time() + 2.0
    remaining = set(targets)
    while remaining and time.time() < deadline:
        alive: set[int] = set()
        for pid in remaining:
            if os.path.exists(f"/proc/{pid}"):
                alive.add(pid)
        remaining = alive
        if remaining:
            time.sleep(0.1)

    # Force kill anything that ignored SIGTERM.
    for pid in list(remaining):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError:
            logger.warning(
                "No permission to force-kill stale ccbot pid=%d",
                pid,
            )
        except OSError as e:
            logger.warning("Failed to force-kill stale ccbot pid=%d: %s", pid, e)

    logger.info("Terminated stale ccbot instances: %s", ", ".join(map(str, targets)))


def main() -> None:
    """Main entry point."""
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        from .hook import hook_main

        hook_main()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "codex-map":
        from .codex_mapper import codex_session_mapper

        changed = asyncio.run(codex_session_mapper.sync_session_map())
        print("updated" if changed else "no changes")
        return

    forward_ports = _parse_forward_ports(sys.argv[1:])
    if forward_ports:
        os.environ["CCBOT_FORWARD_PORTS"] = ",".join(str(p) for p in forward_ports)

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    # Import config before enabling DEBUG — avoid leaking debug logs on config errors
    try:
        from .config import config
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        sys.exit(1)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    # AIORateLimiter (max_retries=5) handles retries itself; keep INFO for visibility
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    _terminate_other_ccbot_instances(logger)

    from .tmux_manager import tmux_manager

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Provider: %s", config.provider)
    logger.info("Provider data root: %s", config.provider_data_root)

    # Ensure tmux session exists
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting Telegram bot...")
    from .bot import create_bot

    application = create_bot()
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
