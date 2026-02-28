"""Tests for Codex rollout parsing in TranscriptParser."""

from ccbot.transcript_parser import TranscriptParser

EXPQUOTE_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXPQUOTE_END = TranscriptParser.EXPANDABLE_QUOTE_END


class TestCodexParseEntries:
    def test_skips_system_entries(self):
        entries = [
            {"type": "session_meta", "payload": {"id": "s1", "cwd": "/tmp"}},
            {"type": "turn_context", "payload": {"cwd": "/tmp"}},
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        assert result == []
        assert pending == {}

    def test_parses_assistant_message_and_reasoning(self):
        entries = [
            {
                "type": "response_item",
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello"}],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-01-01T00:00:01Z",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "thinking"}],
                },
            },
        ]
        result, _ = TranscriptParser.parse_entries(entries)
        assert len(result) == 2
        assert result[0].role == "assistant"
        assert result[0].content_type == "text"
        assert result[0].text == "hello"
        assert result[1].content_type == "thinking"
        assert EXPQUOTE_START in result[1].text and EXPQUOTE_END in result[1].text

    def test_parses_tool_call_and_output_pair(self):
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"cmd":"git status"}',
                    "call_id": "call-1",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "On branch main",
                },
            },
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        assert pending == {}
        tool_use = [e for e in result if e.content_type == "tool_use"]
        tool_result = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_use) == 1
        assert len(tool_result) == 1
        assert "exec_command" in tool_use[0].text
        assert "On branch main" in tool_result[0].text

    def test_parses_event_user_message(self):
        entries = [
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "привет"},
            }
        ]
        result, _ = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].role == "user"
        assert result[0].text == "привет"
