#!/usr/bin/env python3
"""Bidirectional bridge: local Codex TTY <-> Telegram chat (without tmux).

Runs Codex inside a PTY and mirrors:
- local keyboard input -> Codex + optional Telegram log
- Codex terminal output -> local terminal + Telegram
- Telegram messages -> Codex + local terminal marker

Designed for Linux/WSL terminals.
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import os
import pty
import re
import signal
import sys
import termios
import tty
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1B\].*?(?:\x07|\x1B\\)")

SAFE_TELEGRAM_CHUNK = 3500


@dataclass(slots=True)
class BridgeConfig:
    token: str
    chat_id: int | None
    allowed_user_id: int | None
    poll_timeout: int
    send_interval: float
    mirror_local_input: bool
    raw_input: bool
    command: list[str]


class TelegramClient:
    def __init__(self, token: str, chat_id: int | None) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.client = httpx.AsyncClient(timeout=40.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def send_text(self, text: str) -> None:
        if not text or self.chat_id is None:
            return
        for part in _chunk_text(text, SAFE_TELEGRAM_CHUNK):
            payload = {
                "chat_id": self.chat_id,
                "text": part,
                "disable_web_page_preview": True,
            }
            try:
                resp = await self.client.post(f"{self.base}/sendMessage", json=payload)
                resp.raise_for_status()
                body = resp.json()
                if not body.get("ok", False):
                    desc = body.get("description", "unknown Telegram API error")
                    print(f"[bridge] Telegram send failed: {desc}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"[bridge] Telegram send exception: {e}", file=sys.stderr)

    async def get_updates(self, offset: int | None, timeout: int) -> tuple[list[dict], int | None]:
        payload: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset

        try:
            resp = await self.client.post(f"{self.base}/getUpdates", json=payload)
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:  # noqa: BLE001
            print(f"[bridge] Telegram polling exception: {e}", file=sys.stderr)
            await asyncio.sleep(2.0)
            return [], offset

        if not body.get("ok", False):
            desc = body.get("description", "unknown Telegram API error")
            print(f"[bridge] Telegram getUpdates failed: {desc}", file=sys.stderr)
            await asyncio.sleep(2.0)
            return [], offset

        results = body.get("result", [])
        if not isinstance(results, list):
            return [], offset

        next_offset = offset
        out: list[dict] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            update_id = item.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1
            out.append(item)

        return out, next_offset


class CodexBridge:
    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self.telegram = TelegramClient(cfg.token, cfg.chat_id)

        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()

        self.child_pid: int | None = None
        self.master_fd: int | None = None
        self.old_tty: list | None = None

        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.stdout_buf = ""
        self.last_sent_line = ""
        self.local_line_buf = bytearray()

        self.send_queue: asyncio.Queue[str] = asyncio.Queue()
        self.tasks: list[asyncio.Task] = []

    async def run(self) -> int:
        try:
            self._spawn_child()
            self._configure_local_tty()
            self._install_signal_handlers()
            self._install_fd_readers()

            # Clear pending backlog so bridge starts from "now" messages only.
            await self._drain_existing_updates()
            await self.send_queue.put("[bridge] connected: local <-> codex <-> telegram")

            self.tasks = [
                asyncio.create_task(self._telegram_sender(), name="sender"),
                asyncio.create_task(self._telegram_poller(), name="poller"),
                asyncio.create_task(self._stdout_flush_loop(), name="flush"),
                asyncio.create_task(self._wait_child_exit(), name="wait-child"),
            ]

            await self.stop_event.wait()
            return 0
        finally:
            for t in self.tasks:
                t.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()

            self._cleanup_fd_readers()
            self._restore_local_tty()

            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
                self.master_fd = None

            await self.telegram.close()

    def _spawn_child(self) -> None:
        pid, master_fd = pty.fork()
        if pid == 0:
            # Child process: replace with Codex command.
            os.execvp(self.cfg.command[0], self.cfg.command)
        self.child_pid = pid
        self.master_fd = master_fd

    def _configure_local_tty(self) -> None:
        if not sys.stdin.isatty():
            raise SystemExit("stdin is not a TTY; run this bridge from interactive terminal")
        if self.cfg.raw_input:
            fd = sys.stdin.fileno()
            self.old_tty = termios.tcgetattr(fd)
            tty.setraw(fd)

    def _restore_local_tty(self) -> None:
        if self.old_tty is None:
            # Best-effort: restore visible cursor / main screen in case child left TUI state.
            try:
                os.write(sys.stdout.fileno(), b"\x1b[?25h\x1b[?1049l")
            except OSError:
                pass
            return
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_tty)
        except OSError:
            pass
        try:
            os.write(sys.stdout.fileno(), b"\x1b[?25h\x1b[?1049l")
        except OSError:
            pass
        self.old_tty = None

    def _install_signal_handlers(self) -> None:
        try:
            self.loop.add_signal_handler(signal.SIGINT, self._forward_ctrl_c)
        except NotImplementedError:
            pass
        try:
            self.loop.add_signal_handler(signal.SIGTERM, self._request_stop)
        except NotImplementedError:
            pass

    def _install_fd_readers(self) -> None:
        if self.master_fd is None:
            raise RuntimeError("master fd not initialized")

        self.loop.add_reader(self.master_fd, self._on_codex_output)
        self.loop.add_reader(sys.stdin.fileno(), self._on_local_input)

    def _cleanup_fd_readers(self) -> None:
        if self.master_fd is not None:
            self.loop.remove_reader(self.master_fd)
        try:
            self.loop.remove_reader(sys.stdin.fileno())
        except Exception:  # noqa: BLE001
            pass

    async def _wait_child_exit(self) -> None:
        if self.child_pid is None:
            return
        _, status = await asyncio.to_thread(os.waitpid, self.child_pid, 0)
        code = _decode_wait_status(status)
        await self.send_queue.put(f"[bridge] codex process exited (code={code})")
        self._request_stop()

    async def _drain_existing_updates(self) -> None:
        offset = None
        for _ in range(2):
            updates, offset = await self.telegram.get_updates(offset=offset, timeout=0)
            if not updates:
                return

    async def _stdout_flush_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(self.cfg.send_interval)
            await self._flush_stdout_buf()

    async def _flush_stdout_buf(self) -> None:
        if not self.stdout_buf:
            return
        text = self.stdout_buf
        self.stdout_buf = ""

        cleaned = _clean_terminal_text(text)
        if not cleaned.strip():
            return

        # Keep messages short and avoid obvious spinner storms.
        lines = [ln.rstrip() for ln in cleaned.splitlines()]
        filtered = [ln for ln in lines if ln.strip()]
        if not filtered:
            return

        if len(filtered) == 1 and filtered[0] == self.last_sent_line:
            return

        payload = "\n".join(filtered[-30:])
        self.last_sent_line = filtered[-1]
        await self.send_queue.put(f"[codex]\n{payload}")

    async def _telegram_sender(self) -> None:
        while not self.stop_event.is_set():
            text = await self.send_queue.get()
            if text:
                await self.telegram.send_text(text)

    async def _telegram_poller(self) -> None:
        offset: int | None = None
        while not self.stop_event.is_set():
            updates, offset = await self.telegram.get_updates(
                offset=offset,
                timeout=self.cfg.poll_timeout,
            )
            for upd in updates:
                await self._handle_update(upd)

    async def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message")
        if not isinstance(msg, dict):
            return

        chat = msg.get("chat")
        from_user = msg.get("from")
        if not isinstance(chat, dict):
            return
        if not isinstance(from_user, dict):
            return

        chat_id = chat.get("id")
        user_id = from_user.get("id")
        if self.cfg.allowed_user_id is not None and user_id != self.cfg.allowed_user_id:
            return
        if self.cfg.chat_id is None:
            # Auto-bind to the first valid chat if --chat-id is not provided.
            if not isinstance(chat_id, int):
                return
            self.cfg.chat_id = chat_id
            self.telegram.chat_id = chat_id
            await self.send_queue.put(f"[bridge] auto-bound to chat_id={chat_id}")
        elif chat_id != self.cfg.chat_id:
            return

        text = msg.get("text")
        if not isinstance(text, str):
            return

        text = text.strip()
        if not text:
            return

        if text == "/help":
            await self.send_queue.put(
                "[bridge] commands: /help, /esc, /ctrlc, /enter, /quit\n"
                "Any other text is sent to codex as a new line."
            )
            return

        if text == "/esc":
            self._write_to_codex(b"\x1b")
            self._print_local_marker("[tg] sent ESC")
            return

        if text == "/ctrlc":
            self._write_to_codex(b"\x03")
            self._print_local_marker("[tg] sent Ctrl-C")
            return

        if text == "/enter":
            self._write_to_codex(b"\n")
            self._print_local_marker("[tg] sent Enter")
            return

        if text == "/quit":
            self._print_local_marker("[tg] requested stop")
            self._request_stop()
            return

        payload = (text + "\n").encode("utf-8", errors="replace")
        self._write_to_codex(payload)
        self._print_local_marker(f"[tg] {text}")

    def _on_codex_output(self) -> None:
        if self.master_fd is None:
            return

        try:
            chunk = os.read(self.master_fd, 4096)
        except OSError:
            self._request_stop()
            return

        if not chunk:
            self._request_stop()
            return

        try:
            os.write(sys.stdout.fileno(), chunk)
        except OSError:
            pass

        decoded = self.decoder.decode(chunk)
        self.stdout_buf += decoded

    def _on_local_input(self) -> None:
        if self.master_fd is None:
            return

        try:
            data = os.read(sys.stdin.fileno(), 1024)
        except OSError:
            self._request_stop()
            return

        if not data:
            self._request_stop()
            return

        self._write_to_codex(data)
        if self.cfg.mirror_local_input:
            self._track_local_line(data)

    def _track_local_line(self, data: bytes) -> None:
        for b in data:
            if b in (10, 13):
                if self.local_line_buf:
                    line = self.local_line_buf.decode("utf-8", errors="ignore").strip()
                    self.local_line_buf.clear()
                    if line:
                        self.send_queue.put_nowait(f"[local] {line}")
                continue
            if b in (8, 127):
                if self.local_line_buf:
                    self.local_line_buf.pop()
                continue
            if b == 3:
                self.send_queue.put_nowait("[local] Ctrl-C")
                continue
            if 32 <= b <= 126:
                self.local_line_buf.append(b)

    def _write_to_codex(self, data: bytes) -> None:
        if self.master_fd is None:
            return
        try:
            os.write(self.master_fd, data)
        except OSError:
            self._request_stop()

    def _print_local_marker(self, text: str) -> None:
        try:
            os.write(sys.stdout.fileno(), f"\r\n{text}\r\n".encode("utf-8"))
        except OSError:
            pass

    def _request_stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.child_pid is not None:
            try:
                os.kill(self.child_pid, signal.SIGTERM)
            except OSError:
                pass

    def _forward_ctrl_c(self) -> None:
        # In line-mode terminals Ctrl-C raises SIGINT instead of sending \x03.
        # Forward it into Codex first; use /quit (Telegram) or SIGTERM to stop bridge.
        self._write_to_codex(b"\x03")
        self._print_local_marker("[local] Ctrl-C -> codex")


def _decode_wait_status(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            for i in range(0, len(line), limit):
                out.append(line[i : i + limit])
            continue
        if len(cur) + len(line) > limit and cur:
            out.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        out.append(cur)
    return out


def _clean_terminal_text(text: str) -> str:
    text = OSC_RE.sub("", text)
    text = ANSI_RE.sub("", text)
    # Turn carriage-return redraws into new lines for Telegram readability.
    text = text.replace("\r", "\n")
    # Keep only printable-ish text/newlines/tabs.
    filtered = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch.isprintable())
    return filtered


def _parse_args(argv: Sequence[str]) -> BridgeConfig:
    parser = argparse.ArgumentParser(
        prog="codex-telegram-bridge",
        description="Mirror one Codex session between local terminal and Telegram (without tmux)",
    )
    parser.add_argument("--token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument(
        "--chat-id",
        type=int,
        default=int(os.environ.get("TELEGRAM_CHAT_ID", "0") or "0"),
    )
    parser.add_argument(
        "--allowed-user-id",
        type=int,
        default=(
            int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
            if os.environ.get("TELEGRAM_ALLOWED_USER_ID")
            else None
        ),
    )
    parser.add_argument("--poll-timeout", type=int, default=25)
    parser.add_argument("--send-interval", type=float, default=1.0)
    parser.add_argument(
        "--path",
        default="",
        help="Working directory for Codex (maps to `codex -C <path>`)",
    )
    parser.add_argument(
        "--guid",
        default="",
        help="Codex session GUID to resume (maps to `codex resume <guid>`)",
    )
    parser.add_argument(
        "--no-mirror-local-input",
        action="store_true",
        help="Do not mirror local entered lines to Telegram",
    )
    parser.add_argument(
        "--raw-input",
        action="store_true",
        help="Use raw TTY mode (advanced; default is stable line-mode)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run (default: codex --no-alt-screen)",
    )

    ns = parser.parse_args(list(argv))

    token = (ns.token or "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required (env or --token)")

    allowed_user_id = ns.allowed_user_id
    if allowed_user_id is None:
        raw_allowed_users = (os.environ.get("ALLOWED_USERS") or "").strip()
        if raw_allowed_users:
            first = raw_allowed_users.split(",")[0].strip()
            if first:
                try:
                    allowed_user_id = int(first)
                except ValueError:
                    pass

    cmd = [c for c in ns.command if c != "--"]
    if not cmd:
        # Default command builder:
        # - codex in a specific path:      codex --no-alt-screen -C <path>
        # - resume by guid in that path:   codex --no-alt-screen -C <path> resume <guid>
        path = (ns.path or "").strip()
        guid = (ns.guid or "").strip()

        cmd = ["codex", "--no-alt-screen"]
        if path:
            cmd.extend(["-C", path])
        if guid:
            cmd.extend(["resume", guid])

    return BridgeConfig(
        token=token,
        chat_id=(None if ns.chat_id == 0 else ns.chat_id),
        allowed_user_id=allowed_user_id,
        poll_timeout=max(1, ns.poll_timeout),
        send_interval=max(0.2, ns.send_interval),
        mirror_local_input=not ns.no_mirror_local_input,
        raw_input=bool(ns.raw_input),
        command=cmd,
    )


def _load_default_env() -> None:
    load_dotenv(override=False)
    load_dotenv(Path.home() / ".ccbot" / ".env", override=False)


def main(argv: Sequence[str] | None = None) -> int:
    _load_default_env()
    cfg = _parse_args(sys.argv[1:] if argv is None else argv)

    print(
        "[bridge] starting with command:",
        " ".join(cfg.command),
        file=sys.stderr,
    )
    print(
        "[bridge] telegram controls: /help /esc /ctrlc /enter /quit",
        file=sys.stderr,
    )

    async def _runner() -> int:
        bridge = CodexBridge(cfg)
        return await bridge.run()

    return asyncio.run(_runner())


if __name__ == "__main__":
    raise SystemExit(main())
