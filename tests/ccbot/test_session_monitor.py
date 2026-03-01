"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json
from unittest.mock import AsyncMock

import pytest

from ccbot.monitor_state import TrackedSession
from ccbot.session_monitor import (
    _MAX_EMITTED_TEXT_CHARS,
    _MAX_INITIAL_BACKLOG_BYTES,
    SessionInfo,
    SessionMonitor,
)
from ccbot.transcript_parser import ParsedEntry


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1


class TestSessionMonitorOversizedProtection:
    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_check_for_updates_truncates_oversized_entry_text(
        self, monitor, tmp_path, monkeypatch
    ):
        session_id = "s1"
        jsonl_file = tmp_path / "session.jsonl"
        jsonl_file.write_text("{}\n", encoding="utf-8")

        tracked = TrackedSession(
            session_id=session_id,
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )
        monitor.state.update_session(tracked)
        monitor._file_mtimes[session_id] = -1.0

        async def fake_scan_projects():
            return [SessionInfo(session_id=session_id, file_path=jsonl_file)]

        async def fake_read_new_lines(_tracked, _file):
            return [{"__ccbot_line_start": 10, "__ccbot_line_end": 20}]

        huge_text = "X" * (_MAX_EMITTED_TEXT_CHARS + 777)

        def fake_parse_entries(_entries, pending_tools=None):
            return (
                [ParsedEntry(role="assistant", text=huge_text, content_type="text")],
                pending_tools or {},
            )

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)
        monkeypatch.setattr(monitor, "_read_new_lines", fake_read_new_lines)
        monkeypatch.setattr(
            "ccbot.session_monitor.TranscriptParser.parse_entries",
            fake_parse_entries,
        )

        messages = await monitor.check_for_updates({session_id})
        assert len(messages) == 1
        assert len(messages[0].text) < len(huge_text)
        assert "truncated by CCBot" in messages[0].text

    @pytest.mark.asyncio
    async def test_check_for_updates_fast_forwards_large_startup_backlog(
        self, monitor, tmp_path, monkeypatch
    ):
        session_id = "s2"
        jsonl_file = tmp_path / "session.jsonl"
        payload = "A" * (_MAX_INITIAL_BACKLOG_BYTES + 100)
        jsonl_file.write_text(payload, encoding="utf-8")

        tracked = TrackedSession(
            session_id=session_id,
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )
        monitor.state.update_session(tracked)
        monitor._file_mtimes[session_id] = -1.0

        async def fake_scan_projects():
            return [SessionInfo(session_id=session_id, file_path=jsonl_file)]

        mock_read_new_lines = AsyncMock(return_value=[])
        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)
        monkeypatch.setattr(monitor, "_read_new_lines", mock_read_new_lines)

        messages = await monitor.check_for_updates({session_id})
        assert messages == []
        assert tracked.last_byte_offset == jsonl_file.stat().st_size
        mock_read_new_lines.assert_not_called()
