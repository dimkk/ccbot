"""Bridge local stdin + Telegram to Codex app-server (JSON-RPC over stdio).

This is a persistent session bridge (thread + turns), unlike exec-per-message mode.
It can stream item-level progress (reasoning/commands/messages) similar to TUI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

SAFE_TELEGRAM_CHUNK = 3500


class AppServerError(Exception):
    pass


@dataclass(slots=True)
class BridgeConfig:
    token: str
    chat_id: int | None
    allowed_user_id: int | None
    poll_timeout: int
    path: str
    guid: str | None
    model: str | None
    no_telegram: bool


@dataclass(slots=True)
class InputMessage:
    source: str  # "local" | "tg"
    text: str


class TelegramClient:
    def __init__(self, token: str, chat_id: int | None) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self._client_lock = asyncio.Lock()
        self.client = self._new_client()

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(40.0, connect=20.0),
            http2=False,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def close(self) -> None:
        async with self._client_lock:
            await self.client.aclose()

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


class AppServerClient:
    def __init__(self, model: str | None, env: dict[str, str]) -> None:
        self.model = model
        self.env = env

        self.proc: asyncio.subprocess.Process | None = None
        self.notifications: asyncio.Queue[dict] = asyncio.Queue()

        self._pending: dict[str, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        self._reader_task = asyncio.create_task(self._stdout_loop(), name="app-server-stdout")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="app-server-stderr")

        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "ccbot-app-server-bridge",
                    "title": "CCBot AppServer Bridge",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                },
            },
            timeout=20.0,
        )
        await self.notify("initialized", None)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        await asyncio.gather(*(t for t in (self._reader_task, self._stderr_task) if t is not None), return_exceptions=True)

        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()

    async def request(self, method: str, params: dict | None, timeout: float = 120.0) -> dict:
        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut

        msg: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        await self._send_json(msg)

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

        if not isinstance(result, dict):
            raise AppServerError(f"Invalid response for {method}: {result!r}")
        return result

    async def notify(self, method: str, params: dict | None) -> None:
        msg: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        await self._send_json(msg)

    async def _send_json(self, msg: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise AppServerError("app-server stdin is unavailable")
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            self.proc.stdin.write(line.encode("utf-8"))
            await self.proc.stdin.drain()

    async def _stdout_loop(self) -> None:
        if self.proc is None or self.proc.stdout is None:
            return

        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[bridge] app-server non-json line: {raw}", file=sys.stderr)
                continue

            # Response or error to our request
            if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
                req_id = str(msg.get("id"))
                fut = self._pending.get(req_id)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    fut.set_exception(AppServerError(str(msg["error"])))
                else:
                    fut.set_result(msg["result"])
                continue

            # Server -> client request (must be answered)
            if "id" in msg and "method" in msg:
                await self._handle_server_request(msg)
                continue

            # Notification
            if "method" in msg:
                await self.notifications.put(msg)
                continue

    async def _stderr_loop(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"[app-server] {text}", file=sys.stderr)

    async def _handle_server_request(self, msg: dict) -> None:
        req_id = msg.get("id")
        method = msg.get("method")

        # Auto-approve in full-permission mode.
        mapping: dict[str, dict] = {
            "item/commandExecution/requestApproval": {"decision": "acceptForSession"},
            "item/fileChange/requestApproval": {"decision": "acceptForSession"},
            "execCommandApproval": {"decision": "approved_for_session"},
            "applyPatchApproval": {"decision": "approved_for_session"},
        }

        if method in mapping:
            response = {"jsonrpc": "2.0", "id": req_id, "result": mapping[method]}
            await self._send_json(response)
            return

        # For unsupported request types, return a JSON-RPC method-not-found error.
        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"unsupported server request: {method}",
            },
        }
        await self._send_json(response)


class CodexAppServerBridge:
    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()

        self.telegram: TelegramClient | None = None
        if not cfg.no_telegram:
            self.telegram = TelegramClient(cfg.token, cfg.chat_id)

        self.input_queue: asyncio.Queue[InputMessage] = asyncio.Queue()
        self.telegram_send_queue: asyncio.Queue[str] = asyncio.Queue()
        self.child_env = _build_child_env()

        self.app = AppServerClient(model=cfg.model, env=self.child_env)

        self.thread_id: str | None = None
        self.active_turn_id: str | None = None

        self.tasks: list[asyncio.Task] = []

    async def run(self) -> int:
        try:
            await self.app.start()
            await self._start_or_resume_thread()

            self.tasks = [
                asyncio.create_task(self._local_input_loop(), name="local-input"),
                asyncio.create_task(self._input_worker_loop(), name="input-worker"),
            ]

            if self.telegram is not None:
                await self._drain_existing_updates()
                self.tasks.append(asyncio.create_task(self._telegram_sender_loop(), name="tg-sender"))
                self.tasks.append(asyncio.create_task(self._telegram_poller_loop(), name="tg-poller"))
                await self.telegram_send_queue.put("Мост app-server запущен.")

            await self.stop_event.wait()
            return 0
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()

            await self.app.close()
            if self.telegram is not None:
                await self.telegram.close()

    async def _start_or_resume_thread(self) -> None:
        params: dict[str, object] = {
            "cwd": self.cfg.path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if self.cfg.model:
            params["model"] = self.cfg.model

        if self.cfg.guid:
            resp = await self._request_with_retries(
                "thread/resume",
                {
                    "threadId": self.cfg.guid,
                    **params,
                },
                timeout=30.0,
                attempts=4,
            )
        else:
            resp = await self._request_with_retries(
                "thread/start",
                params,
                timeout=30.0,
                attempts=4,
            )

        thread = resp.get("thread") if isinstance(resp, dict) else None
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise AppServerError(f"invalid thread response: {resp!r}")
        self.thread_id = thread["id"]
        print(f"[bridge] thread: {self.thread_id}", file=sys.stderr)

    async def _request_with_retries(
        self,
        method: str,
        params: dict,
        timeout: float,
        attempts: int,
    ) -> dict:
        last_exc: Exception | None = None
        for i in range(1, attempts + 1):
            try:
                return await self.app.request(method, params, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                last_exc = e
                transient = _is_transient_network_error(e)
                if i >= attempts or not transient:
                    raise
                delay = min(8.0, 1.5 * (2 ** (i - 1)))
                msg = f"Проблема сети, повторяю попытку {i + 1}/{attempts}..."
                if self.telegram is not None:
                    await self.telegram_send_queue.put(msg)
                print(f"[bridge] retry {method} ({i}/{attempts}) after error: {e}", file=sys.stderr)
                await asyncio.sleep(delay)
        if last_exc is not None:
            raise last_exc
        raise AppServerError(f"{method} failed without error")

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
            await self.telegram_send_queue.put("Подключился к этому чату.")
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

        if self.telegram is not None:
            if item.source == "tg":
                await self.telegram_send_queue.put("Принял. Запускаю новый turn.")
            else:
                await self.telegram_send_queue.put(f"Ты (консоль): {text}")

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
                "/setguid <GUID> (только локально, с перезапуском thread)\n"
                "/interrupt\n"
                "/quit",
            )
            return

        if cmd == "/guid":
            await self._reply_to_source(source, self.thread_id or "(none)")
            return

        if cmd == "/setguid":
            if source != "local":
                await self._reply_to_source(source, "Команда доступна только локально.")
                return
            if not arg:
                await self._reply_to_source(source, "Использование: /setguid <GUID>")
                return
            self.cfg.guid = arg
            await self._reply_to_source(source, "GUID сохранен. Перезапусти мост для применения.")
            return

        if cmd == "/interrupt":
            if not self.thread_id or not self.active_turn_id:
                await self._reply_to_source(source, "Сейчас нет активного turn.")
                return
            await self.app.request(
                "turn/interrupt",
                {
                    "threadId": self.thread_id,
                    "turnId": self.active_turn_id,
                },
                timeout=20.0,
            )
            await self._reply_to_source(source, "Отправил interrupt.")
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
            await self._run_turn(item)

    async def _run_turn(self, item: InputMessage) -> None:
        if not self.thread_id:
            await self._reply_to_source(item.source, "Нет активной сессии thread.")
            return

        try:
            resp = await self._request_with_retries(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": item.text,
                            "text_elements": [],
                        }
                    ],
                    "approvalPolicy": "never",
                },
                timeout=30.0,
                attempts=4,
            )
        except Exception as e:  # noqa: BLE001
            await self._reply_to_source(item.source, "Не удалось запустить turn.")
            print(f"[bridge] turn/start failed: {e}", file=sys.stderr)
            return

        turn = resp.get("turn") if isinstance(resp, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            await self._reply_to_source(item.source, "Некорректный ответ turn/start.")
            return

        self.active_turn_id = turn_id
        print(f"[bridge] turn started: {turn_id}", file=sys.stderr)

        assistant_messages: list[str] = []
        sent_reasoning = False

        while not self.stop_event.is_set():
            notif = await self.app.notifications.get()
            method = notif.get("method")
            params = notif.get("params")
            if not isinstance(method, str) or not isinstance(params, dict):
                continue

            n_turn_id = params.get("turnId")
            if not isinstance(n_turn_id, str):
                turn_obj = params.get("turn")
                if isinstance(turn_obj, dict):
                    maybe_id = turn_obj.get("id")
                    if isinstance(maybe_id, str):
                        n_turn_id = maybe_id

            # Ignore unrelated turn notifications.
            if isinstance(n_turn_id, str) and n_turn_id != turn_id:
                continue

            if method == "item/started":
                item_obj = params.get("item")
                if isinstance(item_obj, dict):
                    item_type = item_obj.get("type")
                    if item_type == "reasoning" and not sent_reasoning:
                        sent_reasoning = True
                        if self.telegram is not None:
                            await self.telegram_send_queue.put("печатает")
                    elif item_type == "commandExecution":
                        command = item_obj.get("command")
                        if isinstance(command, str) and self.telegram is not None:
                            await self.telegram_send_queue.put(f"Команда: {command}")
                continue

            if method == "item/completed":
                item_obj = params.get("item")
                if not isinstance(item_obj, dict):
                    continue
                item_type = item_obj.get("type")
                if item_type == "agentMessage":
                    text = item_obj.get("text")
                    if isinstance(text, str) and text.strip():
                        assistant_messages.append(text.strip())
                elif item_type == "commandExecution":
                    out = item_obj.get("aggregatedOutput")
                    exit_code = item_obj.get("exitCode")
                    if self.telegram is not None and isinstance(out, str) and out.strip():
                        await self.telegram_send_queue.put(f"Вывод команды:\n{out.strip()}")
                    if self.telegram is not None and exit_code is not None:
                        await self.telegram_send_queue.put(f"Код выхода: {exit_code}")
                continue

            if method == "turn/completed":
                break

        self.active_turn_id = None

        if assistant_messages:
            final_text = "\n\n".join(assistant_messages)
        else:
            final_text = "Пустой ответ."

        print(f"\n{final_text}\n", flush=True)
        if self.telegram is not None:
            await self.telegram_send_queue.put(final_text)

    def _request_stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()


def _build_child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_ALLOWED_USER_ID",
        "ALLOWED_USERS",
    ):
        env.pop(key, None)
    return env


def _is_transient_network_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "failed to lookup address information",
        "temporary failure in name resolution",
        "try again",
        "timed out",
        "connection reset",
        "failed to connect to websocket",
        "connection refused",
        "dns",
    )
    return any(marker in text for marker in markers)


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


def _parse_args(argv: Sequence[str]) -> BridgeConfig:
    parser = argparse.ArgumentParser(
        prog="ccbot --app",
        description="Bridge local+Telegram messages to codex app-server (items/turns)",
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
    parser.add_argument("--path", default=os.getcwd(), help="Workspace path")
    parser.add_argument("--guid", default="", help="Existing thread GUID to resume")
    parser.add_argument("--model", default="", help="Optional model override")
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
        no_telegram=bool(ns.no_telegram),
    )


def _load_default_env() -> None:
    load_dotenv(override=False)
    load_dotenv(Path.home() / ".ccbot" / ".env", override=False)


def app_bridge_main(argv: Sequence[str] | None = None) -> int:
    _load_default_env()
    cfg = _parse_args(sys.argv[1:] if argv is None else argv)

    print("[bridge] mode=app-server", file=sys.stderr)

    async def _runner() -> int:
        bridge = CodexAppServerBridge(cfg)
        return await bridge.run()

    try:
        return asyncio.run(_runner())
    except KeyboardInterrupt:
        return 130


def main(argv: Sequence[str] | None = None) -> int:
    """Backward-compatible alias for script mode."""
    return app_bridge_main(argv)


if __name__ == "__main__":
    raise SystemExit(app_bridge_main())
