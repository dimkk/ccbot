# Codex CLI Support Design for CCBot

## Status

- Branch: `codex-support`
- Scope: design only (no runtime behavior changes yet)

## Goal

Add first-class support for running and monitoring `codex` sessions in tmux, while preserving current Claude workflow.

## Current coupling (baseline)

The current code is tightly coupled to Claude-specific assumptions:

- Launch command: `config.claude_command` (default `claude`)
- Session discovery: `~/.claude/projects/*/*.jsonl`
- Window->session mapping: Claude `SessionStart` hook via `ccbot hook`
- Transcript parser: Claude JSONL schema (`type=user|assistant`, `message.content[]`)
- Bot menu and command UX: Claude slash commands (`/clear`, `/compact`, `/cost`, ...)

Main touchpoints:

- `src/ccbot/config.py`
- `src/ccbot/hook.py`
- `src/ccbot/session.py`
- `src/ccbot/session_monitor.py`
- `src/ccbot/transcript_parser.py`
- `src/ccbot/bot.py`
- `src/ccbot/terminal_parser.py`

## Observed Codex runtime facts (local codex-cli 0.104.0)

- CLI command exists: `codex`
- Session data is written under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- `session_meta` event includes stable fields we can use:
  - `payload.id` (session id)
  - `payload.cwd` (project directory)
- Rollout stream includes structured events we can map to notifications:
  - assistant/user messages (`response_item` -> `message`)
  - tool calls/results (`response_item` -> `function_call` / `function_call_output`)
  - reasoning (`event_msg.agent_reasoning`, `response_item.reasoning`)

## Design principles

- Keep Claude path fully backward compatible.
- Add provider abstraction, not provider-specific conditionals in every module.
- Keep existing `session_map.json` concept as the source of truth for "window -> active session".
- Make Codex integration resilient when no hook API is available.

## Proposed architecture

### 1) Provider abstraction

Introduce runtime provider selection:

- `CCBOT_PROVIDER=claude|codex` (default: `claude`)
- `CCBOT_AGENT_COMMAND` (provider-specific default if unset)

Config model additions:

- `provider: Literal["claude", "codex"]`
- `agent_command: str`
- `provider_data_root: Path` (Claude: `~/.claude/projects`, Codex: `~/.codex/sessions`)
- `provider_supports_hook: bool`

Backward compatibility:

- If `CCBOT_PROVIDER` is unset, existing env vars continue to work exactly as now.
- `CLAUDE_COMMAND` remains supported as alias for `CCBOT_AGENT_COMMAND` when provider is `claude`.

### 2) Session mapping strategy

#### Claude provider

- Keep current hook flow (`ccbot hook`) unchanged.

#### Codex provider

Codex does not provide the same SessionStart hook contract, so add "resolver" mapping:

- New mapper module (`codex_session_mapper.py`) maintains window->session mapping by polling Codex rollout files.
- Match policy for each tmux window:
  1. Match by normalized `cwd` from tmux pane and `session_meta.payload.cwd`
  2. Prefer newest session near window creation/start timestamp
  3. Prevent duplicate claims with an in-memory "claimed session ids" set
- Write results into existing `session_map.json` format, with extra metadata:
  - `provider: "codex"`
  - `file_path: "...rollout-*.jsonl"`

This keeps `session_manager.load_session_map()` usable for both providers.

### 3) Transcript parsing adapters

Split parser into provider-specific adapters behind one interface.

- `claude_transcript_parser.py` (current logic)
- `codex_transcript_parser.py` (new)

Common parsed output remains existing `ParsedEntry` contract:

- `role`, `text`, `content_type`, `tool_use_id`, `tool_name`, `image_data`

Codex mapping rules (MVP):

- `response_item.message(role=assistant)` -> `content_type=text`
- `event_msg.agent_reasoning` / `response_item.reasoning` -> `content_type=thinking`
- `response_item.function_call` -> `content_type=tool_use`
- `response_item.function_call_output` -> `content_type=tool_result`
- `event_msg.user_message` -> `role=user`, `content_type=text`

### 4) Monitor abstraction

Refactor `SessionMonitor` to provider adapters:

- `ProviderMonitor.scan_sessions(active_session_ids)`
- `ProviderMonitor.read_new_entries(session)`
- `ProviderMonitor.parse_entries(entries, pending_state)`

Claude adapter keeps existing project index behavior.
Codex adapter reads direct `rollout-*.jsonl` paths from `session_map` metadata first, fallback to indexed cache.

### 5) Bot UX and command capabilities

Introduce provider capability flags:

- `supports_slash_forwarding`
- `supports_claude_interactive_ui_patterns`
- `supports_usage_command`

Behavior changes:

- For `codex`, hide Claude-specific menu commands by default.
- Keep plain text forwarding unchanged.
- Keep screenshot and escape controls unchanged.
- Keep slash forwarding configurable (`CCBOT_FORWARD_SLASH=true|false`) because Codex command semantics differ.

### 6) Hook/CLI surface

Extend entrypoint modes:

- `ccbot hook` -> Claude hook (existing)
- `ccbot codex-map` -> on-demand codex map sync (for manual run/debug)

No breaking changes to existing `ccbot` and `ccbot hook --install`.

## Implementation plan (phased)

### Phase 1: Config and provider skeleton

- Add provider config and defaults.
- Introduce provider interface and wire through monitor/session manager.
- Keep behavior identical for Claude provider.

Acceptance:

- Existing Claude tests pass without behavior changes.

### Phase 2: Codex mapping MVP

- Add codex mapper that populates `session_map.json` from tmux + rollout metadata.
- Trigger mapper in monitor loop for `provider=codex`.

Acceptance:

- New codex window created by bot gets auto-bound to a codex session id.

### Phase 3: Codex transcript parsing

- Add codex transcript parser.
- Route monitor parsing by provider.

Acceptance:

- Telegram receives assistant text + tool call/result updates from Codex rollout files.

### Phase 4: Bot capability gating

- Hide/adjust Claude-only commands for codex provider.
- Keep generic functionality intact.

Acceptance:

- `start/history/screenshot/esc/kill/unbind` still work under codex provider.
- No misleading Claude-specific command menu when provider is codex.

### Phase 5: Docs and migration guide

- Update README for provider selection and codex setup.
- Add troubleshooting section for ambiguous session mapping.

## Test plan

### Unit tests

- Config provider parsing and default resolution.
- Codex mapper matching logic (cwd + timestamp + collision handling).
- Codex transcript parser fixtures:
  - assistant text
  - reasoning
  - function_call + function_call_output pairing
  - user message events

### Integration tests

- Simulated tmux windows + fake `~/.codex/sessions` fixtures.
- End-to-end monitor cycle emits `NewMessage` objects for codex sessions.

### Regression tests

- Existing Claude tests remain green.
- Existing hook tests remain green.

## Risks and mitigations

- Ambiguous mapping when multiple codex sessions start in same cwd.
  - Mitigation: timestamp proximity + claim lock + explicit rebind command fallback.
- High event volume in rollout files (`token_count`, context snapshots).
  - Mitigation: strict event filtering in parser.
- Provider drift across codex-cli versions.
  - Mitigation: parser tolerant to unknown event types, fixture-based compatibility tests.

## Open questions before implementation

- Should codex mode forward slash commands by default, or require opt-in?
- Do we want one shared `session_map.json` for all providers, or provider-specific map files?
- Should interactive UI parsing be provider-specific from day one, or disabled for codex initially?

## Definition of done

Codex support is considered complete when:

- User can set `CCBOT_PROVIDER=codex` and create a new topic/session from Telegram.
- Bot can auto-track the new codex session and stream assistant/tool updates.
- Core controls (send text, screenshot, esc, kill/unbind, history) work.
- Claude mode remains fully backward compatible.
