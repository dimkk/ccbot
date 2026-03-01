"""Tests for CodexSessionMapper."""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from ccbot.codex_mapper import CodexSessionMapper
from ccbot.tmux_manager import TmuxWindow


def _write_rollout(path, session_id: str, cwd: str, ts: str) -> None:
    payload = {
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "cwd": cwd,
            "timestamp": ts,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


class TestCodexSessionMapper:
    @pytest.mark.asyncio
    async def test_maps_window_to_matching_cwd(self, tmp_path):
        sessions_root = tmp_path / "sessions"
        map_file = tmp_path / "session_map.json"
        cwd = str((tmp_path / "proj").resolve())
        _write_rollout(
            sessions_root / "2026/02/28/rollout-2026-02-28T00-00-00-sid-1.jsonl",
            "sid-1",
            cwd,
            "2026-02-28T00:00:00Z",
        )

        mapper = CodexSessionMapper(
            sessions_root=sessions_root, session_map_file=map_file
        )
        windows = [
            TmuxWindow(
                window_id="@1",
                window_name="proj",
                cwd=cwd,
                pane_current_command="codex",
            )
        ]

        with patch("ccbot.codex_mapper.tmux_manager") as mock_tmux:
            mock_tmux.list_windows = AsyncMock(return_value=windows)
            changed = await mapper.sync_session_map()

        assert changed is True
        data = json.loads(map_file.read_text())
        key = "ccbot:@1"
        assert key in data
        assert data[key]["session_id"] == "sid-1"
        assert data[key]["provider"] == "codex"
        assert data[key]["cwd"] == cwd

    @pytest.mark.asyncio
    async def test_removes_stale_codex_entry(self, tmp_path):
        sessions_root = tmp_path / "sessions"
        map_file = tmp_path / "session_map.json"
        map_file.write_text(
            json.dumps(
                {
                    "ccbot:@99": {
                        "session_id": "sid-old",
                        "cwd": "/tmp/old",
                        "window_name": "old",
                        "provider": "codex",
                        "file_path": "/tmp/old.jsonl",
                    }
                }
            ),
            encoding="utf-8",
        )
        mapper = CodexSessionMapper(
            sessions_root=sessions_root, session_map_file=map_file
        )

        with patch("ccbot.codex_mapper.tmux_manager") as mock_tmux:
            mock_tmux.list_windows = AsyncMock(return_value=[])
            changed = await mapper.sync_session_map()

        assert changed is True
        data = json.loads(map_file.read_text())
        assert "ccbot:@99" not in data

    @pytest.mark.asyncio
    async def test_prefers_resume_session_id_for_main_window(
        self, tmp_path, monkeypatch
    ):
        sessions_root = tmp_path / "sessions"
        map_file = tmp_path / "session_map.json"
        ccbot_cwd = str((tmp_path / "ccbot").resolve())
        resume_sid = "019c9eef-c5f7-7dc2-9e92-de59a1c3cd28"
        other_sid = "019ca375-fbf1-7e20-aff6-38003fd36889"

        resume_path = (
            sessions_root
            / "2026/02/27"
            / f"rollout-2026-02-27T14-50-39-{resume_sid}.jsonl"
        )
        other_path = (
            sessions_root
            / "2026/02/28"
            / f"rollout-2026-02-28T11-55-44-{other_sid}.jsonl"
        )
        _write_rollout(
            resume_path,
            resume_sid,
            str((tmp_path / "opticlaw").resolve()),
            "2026-02-27T14:50:39Z",
        )
        _write_rollout(
            other_path,
            other_sid,
            ccbot_cwd,
            "2026-02-28T11:55:44Z",
        )
        os.utime(resume_path, (1700000000, 1700000000))
        os.utime(other_path, (1800000000, 1800000000))

        mapper = CodexSessionMapper(
            sessions_root=sessions_root, session_map_file=map_file
        )
        windows = [
            TmuxWindow(
                window_id="@3",
                window_name="ccbot",
                cwd=ccbot_cwd,
                pane_current_command="node",
            )
        ]

        monkeypatch.setattr(
            "ccbot.codex_mapper.config.codex_resume_session_id", resume_sid
        )
        monkeypatch.setattr("ccbot.codex_mapper.config.tmux_session_name", "ccbot")

        with patch("ccbot.codex_mapper.tmux_manager") as mock_tmux:
            mock_tmux.list_windows = AsyncMock(return_value=windows)
            changed = await mapper.sync_session_map()

        assert changed is True
        data = json.loads(map_file.read_text())
        assert data["ccbot:@3"]["session_id"] == resume_sid
