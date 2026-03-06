#!/usr/bin/env python3
"""Protocol-mode bridge: local stdin + Telegram -> codex exec --json session.

No tmux, no PTY/TUI. Each incoming message runs one non-interactive Codex turn:
- start new thread (codex exec --json) or
- resume existing thread (codex exec resume --json <guid> ...)

The latest thread GUID is tracked and can be reused for subsequent turns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

SAFE_TELEGRAM_CHUNK = 3500


@dataclass(slots=True)
class BridgeConfig:
    token: str
    chat_id: int | None
    allowed_user_id: int | None
    poll_timeout: int
    path: str
    guid: str | None
    model: str | None
    full_auto: bool
    dangerous: bool
    no_telegram: bool


class TelegramClient:
    def __init__(self, token: str, chat_id: int | None) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self._client_lock = asyncio.Lock()
        self.client = self._new_client()

    async def close(self) -> None:
        async with self._client_lock:
            await self.client.aclose()

    def _new_client(self) -> httpx.AsyncClient:
        # Keep transport conservative; avoids flaky TLS state on some networks.
        return httpx.AsyncClient(
            timeout=httpx.Timeout(40.0, connect=20.0),
            http2=False,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def _reset_client(self) -> None:
        async with self._client_lock:
            old_client = self.client
            self.client = self._new_client()
        try:
            await old_client.aclose()
        except Exception:  # noqa: BLE001
            pass

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
                await self._reset_client()

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
            await self._reset_client()
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


@dataclass(slots=True)
class InputMessage:
    source: str  # "local" | "tg"
    text: str


class CodexProtocolBridge:
    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()

        self.telegram: TelegramClient | None = None
        if not cfg.no_telegram:
            self.telegram = TelegramClient(cfg.token, cfg.chat_id)

        self.current_guid: str | None = cfg.guid
        self.input_queue: asyncio.Queue[InputMessage] = asyncio.Queue()
        self.telegram_send_queue: asyncio.Queue[str] = asyncio.Queue()
        self.child_env = _build_child_env()

        self.current_proc: asyncio.subprocess.Process | None = None
        self.tasks: list[asyncio.Task] = []

    async def run(self) -> int:
        self._install_signal_handlers()

        self.tasks = [
            asyncio.create_task(self._local_input_loop(), name="local-input"),
            asyncio.create_task(self._input_worker_loop(), name="input-worker"),
        ]

        if self.telegram is not None:
            await self._drain_existing_updates()
            self.tasks.append(asyncio.create_task(self._telegram_sender_loop(), name="tg-sender"))
            self.tasks.append(asyncio.create_task(self._telegram_poller_loop(), name="tg-poller"))
            await self.telegram_send_queue.put("Мост запущен.")

        await self.stop_event.wait()

        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

        if self.telegram is not None:
            await self.telegram.close()

        return 0

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self.loop.add_signal_handler(sig, self._request_stop)
            except NotImplementedError:
                pass

    async def _emit_status(self, text: str) -> None:
        print(text, flush=True)
        if self.telegram is not None:
            await self.telegram_send_queue.put(text)

    async def _local_input_loop(self) -> None:
        while not self.stop_event.is_set():
            line = await asyncio.to_thread(sys.stdin.readline)
            if line == "":
                self._request_stop()
                return
            text = line.strip()
            if not text:
                continue
            await self._handle_incoming(InputMessage(source="local", text=text))

    async def _drain_existing_updates(self) -> None:
        if self.telegram is None:
            return
        offset = None
        for _ in range(2):
            updates, offset = await self.telegram.get_updates(offset=offset, timeout=0)
            if not updates:
                return

    async def _telegram_poller_loop(self) -> None:
        if self.telegram is None:
            return

        offset: int | None = None
        while not self.stop_event.is_set():
            updates, offset = await self.telegram.get_updates(offset=offset, timeout=self.cfg.poll_timeout)
            for upd in updates:
                await self._handle_telegram_update(upd)

    async def _handle_telegram_update(self, upd: dict) -> None:
        if self.telegram is None:
            return
        msg = upd.get("message")
        if not isinstance(msg, dict):
            return

        chat = msg.get("chat")
        from_user = msg.get("from")
        if not isinstance(chat, dict) or not isinstance(from_user, dict):
            return

        chat_id = chat.get("id")
        user_id = from_user.get("id")
        if self.cfg.allowed_user_id is not None and user_id != self.cfg.allowed_user_id:
            return

        if self.cfg.chat_id is None:
            if not isinstance(chat_id, int):
                return
            self.cfg.chat_id = chat_id
            self.telegram.chat_id = chat_id
            await self.telegram_send_queue.put(f"[bridge] auto-bound to chat_id={chat_id}")
        elif chat_id != self.cfg.chat_id:
            return

        text = msg.get("text")
        if not isinstance(text, str):
            return
        text = text.strip()
        if not text:
            return

        await self._handle_incoming(InputMessage(source="tg", text=text))

    async def _handle_incoming(self, item: InputMessage) -> None:
        text = item.text.strip()
        if not text:
            return

        if text.startswith("/"):
            await self._handle_command(item.source, text)
            return

        if item.source == "tg" and self.telegram is not None:
            await self.telegram_send_queue.put("Принял. Отправляю в Codex.")

        await self.input_queue.put(item)

    async def _handle_command(self, source: str, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        arg = arg.strip()

        if cmd in ("/quit", "/exit"):
            await self._reply_to_source(source, "Останавливаю мост.")
            self._request_stop()
            return

        if cmd == "/help":
            await self._reply_to_source(
                source,
                "Команды:\n"
                "/help\n"
                "/guid\n"
                "/new\n"
                "/setguid <GUID>\n"
                "/interrupt\n"
                "/quit",
            )
            return

        if cmd == "/guid":
            await self._reply_to_source(source, f"{self.current_guid or '(none)'}")
            return

        if cmd == "/new":
            self.current_guid = None
            await self._reply_to_source(source, "Ок, следующая реплика начнет новую сессию.")
            return

        if cmd == "/setguid":
            if not arg:
                await self._reply_to_source(source, "Использование: /setguid <GUID>")
                return
            self.current_guid = arg
            await self._reply_to_source(source, "GUID сохранен.")
            return

        if cmd == "/interrupt":
            if self.current_proc is None or self.current_proc.returncode is not None:
                await self._reply_to_source(source, "Сейчас нет активного запроса.")
                return
            self.current_proc.terminate()
            await self._reply_to_source(source, "Остановил текущий запрос.")
            return

        await self._reply_to_source(source, "Неизвестная команда.")

    async def _reply_to_source(self, source: str, text: str) -> None:
        if source == "local":
            print(text, flush=True)
            return
        if source == "tg" and self.telegram is not None:
            await self.telegram_send_queue.put(text)
            return
        print(text, flush=True)

    async def _telegram_sender_loop(self) -> None:
        if self.telegram is None:
            return
        while not self.stop_event.is_set():
            text = await self.telegram_send_queue.get()
            if text:
                await self.telegram.send_text(text)

    async def _input_worker_loop(self) -> None:
        while not self.stop_event.is_set():
            item = await self.input_queue.get()
            await self._run_codex_turn(item)

    def _build_codex_cmd(self, prompt: str) -> list[str]:
        base = ["codex", "exec"]
        if self.cfg.path:
            base.extend(["-C", self.cfg.path])
        if self.cfg.model:
            base.extend(["-m", self.cfg.model])
        if self.cfg.full_auto:
            base.append("--full-auto")
        if self.cfg.dangerous:
            base.append("--dangerously-bypass-approvals-and-sandbox")

        if self.current_guid:
            return [*base, "resume", "--json", self.current_guid, prompt]
        return [*base, "--json", prompt]

    async def _run_codex_turn(self, item: InputMessage) -> None:
        cmd = self._build_codex_cmd(item.text)
        print(f"[bridge] running ({item.source}): {' '.join(cmd[:-1])}", file=sys.stderr, flush=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.child_env,
            )
        except Exception as e:  # noqa: BLE001
            await self._reply_to_source(item.source, "Не удалось запустить Codex.")
            print(f"[bridge] failed to start codex: {e}", file=sys.stderr, flush=True)
            return

        self.current_proc = proc
        slow_notice_task: asyncio.Task | None = None
        if item.source == "tg" and self.telegram is not None:
            slow_notice_task = asyncio.create_task(self._send_slow_notice())

        assistant_texts: list[str] = []

        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                continue

            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue

            evt_type = evt.get("type")
            if evt_type == "thread.started":
                tid = evt.get("thread_id")
                if isinstance(tid, str) and tid:
                    self.current_guid = tid
                continue

            if evt_type == "item.completed":
                item_obj = evt.get("item")
                if not isinstance(item_obj, dict):
                    continue
                item_type = item_obj.get("type")
                if item_type == "agent_message":
                    text = item_obj.get("text")
                    if isinstance(text, str) and text.strip():
                        assistant_texts.append(text.strip())
                continue

        stderr_text = ""
        if proc.stderr is not None:
            err_data = await proc.stderr.read()
            stderr_text = err_data.decode("utf-8", errors="replace").strip()

        rc = await proc.wait()
        self.current_proc = None
        if slow_notice_task is not None:
            slow_notice_task.cancel()
            await asyncio.gather(slow_notice_task, return_exceptions=True)

        if assistant_texts:
            final_text = "\n\n".join(assistant_texts)
        elif rc != 0:
            final_text = "Запрос завершился с ошибкой."
        else:
            final_text = "Пустой ответ."

        print(f"\n{final_text}\n", flush=True)
        if self.telegram is not None:
            await self.telegram_send_queue.put(final_text)

        if rc != 0 and stderr_text:
            print(f"[bridge] codex error (rc={rc}):\n{stderr_text}", file=sys.stderr, flush=True)

    async def _send_slow_notice(self) -> None:
        try:
            await asyncio.sleep(8)
            if self.telegram is not None:
                await self.telegram_send_queue.put("Запрос выполняется, еще работаю.")
        except asyncio.CancelledError:
            return

    def _request_stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.current_proc is not None and self.current_proc.returncode is None:
            try:
                self.current_proc.terminate()
            except ProcessLookupError:
                pass


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


def _build_child_env() -> dict[str, str]:
    """Build child env for codex, stripping Telegram bridge secrets."""
    env = dict(os.environ)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_ALLOWED_USER_ID",
        "ALLOWED_USERS",
    ):
        env.pop(key, None)
    return env


def _parse_args(argv: Sequence[str]) -> BridgeConfig:
    parser = argparse.ArgumentParser(
        prog="codex-protocol-bridge",
        description="Bridge local+Telegram messages to codex exec --json (no tmux, no TUI)",
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
    parser.add_argument("--path", default=os.getcwd(), help="Workspace path for codex -C")
    parser.add_argument("--guid", default="", help="Existing session GUID to resume")
    parser.add_argument("--model", default="", help="Optional model name")
    parser.add_argument("--full-auto", action="store_true", help="Pass --full-auto to codex exec")
    parser.add_argument(
        "--safe",
        action="store_true",
        help="Disable full permissions mode",
    )
    parser.add_argument("--no-telegram", action="store_true", help="Local stdin/stdout only")

    ns = parser.parse_args(list(argv))

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

    token = (ns.token or "").strip()
    if not ns.no_telegram and not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required (env or --token), or use --no-telegram")

    path = os.path.abspath((ns.path or os.getcwd()).strip())
    if not os.path.isdir(path):
        raise SystemExit(f"Invalid --path directory: {path}")

    return BridgeConfig(
        token=token,
        chat_id=(None if ns.chat_id == 0 else ns.chat_id),
        allowed_user_id=allowed_user_id,
        poll_timeout=max(1, ns.poll_timeout),
        path=path,
        guid=((ns.guid or "").strip() or None),
        model=((ns.model or "").strip() or None),
        full_auto=bool(ns.full_auto),
        dangerous=not bool(ns.safe),
        no_telegram=bool(ns.no_telegram),
    )


def _load_default_env() -> None:
    load_dotenv(override=False)
    load_dotenv(Path.home() / ".ccbot" / ".env", override=False)


def main(argv: Sequence[str] | None = None) -> int:
    _load_default_env()
    cfg = _parse_args(sys.argv[1:] if argv is None else argv)

    print(
        "[bridge] protocol mode started",
        file=sys.stderr,
    )

    async def _runner() -> int:
        bridge = CodexProtocolBridge(cfg)
        return await bridge.run()

    try:
        return asyncio.run(_runner())
    except KeyboardInterrupt:
        # Fallback for environments where loop signal handlers are unavailable.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
