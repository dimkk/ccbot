"""Tests for message queue pressure and catch-up helpers."""

import asyncio
import time

import pytest

from ccbot.handlers import message_queue
from ccbot.handlers.message_queue import MessageTask


@pytest.fixture(autouse=True)
def clean_message_queue_state() -> None:
    message_queue._message_queues.clear()
    message_queue._queue_locks.clear()
    message_queue._queue_workers.clear()
    message_queue._flood_until.clear()
    message_queue._status_msg_info.clear()
    message_queue._tool_msg_ids.clear()
    yield
    message_queue._message_queues.clear()
    message_queue._queue_locks.clear()
    message_queue._queue_workers.clear()
    message_queue._flood_until.clear()
    message_queue._status_msg_info.clear()
    message_queue._tool_msg_ids.clear()


def test_get_queue_pressure_counts_send_ops() -> None:
    user_id = 101
    q: asyncio.Queue[MessageTask] = asyncio.Queue()
    message_queue._message_queues[user_id] = q

    q.put_nowait(
        MessageTask(
            task_type="content",
            parts=["a", "b", "c"],
            image_data=[("image/png", b"123")],
            created_at=time.monotonic() - 5.0,
        )
    )
    q.put_nowait(MessageTask(task_type="status_update", text="running"))

    task_count, send_ops, oldest_age_seconds = message_queue.get_queue_pressure(user_id)

    assert task_count == 2
    assert send_ops == 5
    assert oldest_age_seconds >= 4.0


def test_is_catchup_pressure_by_age(monkeypatch) -> None:
    monkeypatch.setattr(message_queue.config, "codex_catchup_threshold", 10)

    assert message_queue.is_catchup_pressure(1, 1, 10.1) is True
    assert message_queue.is_catchup_pressure(1, 11, 0.0) is True
    assert message_queue.is_catchup_pressure(11, 1, 0.0) is True
    assert message_queue.is_catchup_pressure(1, 1, 9.9) is False
