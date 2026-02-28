"""Tests for CodexSessionMapper."""

import json
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

        mapper = CodexSessionMapper(sessions_root=sessions_root, session_map_file=map_file)
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
        mapper = CodexSessionMapper(sessions_root=sessions_root, session_map_file=map_file)

        with patch("ccbot.codex_mapper.tmux_manager") as mock_tmux:
            mock_tmux.list_windows = AsyncMock(return_value=[])
            changed = await mapper.sync_session_map()

        assert changed is True
        data = json.loads(map_file.read_text())
        assert "ccbot:@99" not in data
