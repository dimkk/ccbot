"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/Claude paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > $CCBOT_DIR/.env (default ~/.ccbot).
The module-level `config` instance is imported by nearly every other module.

Key class: Config (singleton instantiated as `config`).
"""

import logging
import os
import re
import shlex
from pathlib import Path

from dotenv import load_dotenv

from .utils import ccbot_dir

logger = logging.getLogger(__name__)

# Env vars that must not leak to child processes (e.g. Claude Code via tmux)
SENSITIVE_ENV_VARS = {"TELEGRAM_BOT_TOKEN", "ALLOWED_USERS", "OPENAI_API_KEY"}


def _extract_codex_resume_session_id(command: str) -> str:
    """Extract session id from a command containing `resume <session_id>`."""
    if not command:
        return ""

    match = re.search(r"(?:^|\s)resume\s+([0-9a-fA-F-]{8,})\b", command)
    if match:
        return match.group(1)
    return ""


def _normalize_codex_resume_command(command: str, cwd: Path) -> str:
    """Ensure minimal `codex resume <sid>` gets stable non-interactive defaults.

    When resuming by explicit session id, Codex may ask interactive questions
    about cwd/approvals. For CCBot this creates noisy Telegram interactive
    prompts. Inject defaults only when missing:
      - `-C <cwd>`
      - `-a never`
      - `--sandbox workspace-write`
    """
    if not command:
        return command

    sid = _extract_codex_resume_session_id(command)
    if not sid:
        return command

    try:
        parts = shlex.split(command)
    except ValueError:
        # Keep original command if shell syntax is malformed.
        return command

    codex_idx = -1
    for i, token in enumerate(parts):
        base = os.path.basename(token)
        if base == "codex" or base == "codex.exe":
            codex_idx = i
            break
    if codex_idx < 0:
        return command

    has_cd = False
    has_approval = False
    has_sandbox = False
    for i, token in enumerate(parts):
        if token in ("-C", "--cd") or token.startswith("--cd="):
            has_cd = True
        if (
            token in ("-a", "--ask-for-approval")
            or token.startswith("--ask-for-approval=")
        ):
            has_approval = True
        if (
            token in ("-s", "--sandbox")
            or token.startswith("--sandbox=")
            or token in ("--full-auto", "--dangerously-bypass-approvals-and-sandbox")
        ):
            has_sandbox = True
        # Handle short options with separate value.
        if token == "-C" and i + 1 < len(parts):
            has_cd = True
        if token == "-a" and i + 1 < len(parts):
            has_approval = True
        if token == "-s" and i + 1 < len(parts):
            has_sandbox = True

    insert_tokens: list[str] = []
    if not has_cd:
        insert_tokens.extend(["-C", str(cwd.resolve())])
    if not has_approval:
        insert_tokens.extend(["-a", "never"])
    if not has_sandbox:
        insert_tokens.extend(["--sandbox", "workspace-write"])

    if not insert_tokens:
        return command

    normalized = parts[: codex_idx + 1] + insert_tokens + parts[codex_idx + 1 :]
    return shlex.join(normalized)


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccbot_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir
        # load_dotenv default override=False means first-loaded wins
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "ccbot")
        self.tmux_main_window_name = "__main__"

        provider = os.getenv("CCBOT_PROVIDER", "claude").strip().lower()
        if provider not in ("claude", "codex"):
            raise ValueError("CCBOT_PROVIDER must be one of: claude, codex")
        self.provider = provider

        # Agent command to run in new windows.
        # Backward compatible:
        #   - CLAUDE_COMMAND still works for provider=claude
        #   - CCBOT_AGENT_COMMAND overrides provider defaults
        default_cmd = "codex" if self.provider == "codex" else "claude"
        self.agent_command = os.getenv("CCBOT_AGENT_COMMAND") or os.getenv(
            "CLAUDE_COMMAND", default_cmd
        )
        if self.provider == "codex":
            normalized = _normalize_codex_resume_command(self.agent_command, Path.cwd())
            if normalized != self.agent_command:
                logger.info(
                    "Normalized codex resume command for non-interactive mode: %s",
                    normalized,
                )
            self.agent_command = normalized
        self.codex_resume_session_id = (
            _extract_codex_resume_session_id(self.agent_command)
            if self.provider == "codex"
            else ""
        )
        # Keep old attribute name for compatibility in the existing code path.
        self.claude_command = self.agent_command

        # Provider capabilities and UI labels
        self.agent_name = "Codex CLI" if self.provider == "codex" else "Claude Code"
        self.supports_usage_command = self.provider == "claude"
        # Interactive terminal prompts (approval/select) are used by both providers.
        self.supports_claude_interactive_ui = self.provider in ("claude", "codex")
        # Forward unknown slash commands for both providers by default:
        # e.g. /status, /permissions, /clear, /compact.
        slash_default = "true"
        self.forward_slash_commands = (
            os.getenv("CCBOT_FORWARD_SLASH", slash_default).lower() == "true"
        )

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"

        # Claude Code session monitoring configuration
        # Support custom projects path for Claude variants (e.g., cc-mirror, zai)
        # Priority: CCBOT_CLAUDE_PROJECTS_PATH > CLAUDE_CONFIG_DIR/projects > default
        custom_projects_path = os.getenv("CCBOT_CLAUDE_PROJECTS_PATH")
        claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")

        if custom_projects_path:
            self.claude_projects_path = Path(custom_projects_path)
        elif claude_config_dir:
            self.claude_projects_path = Path(claude_config_dir) / "projects"
        else:
            self.claude_projects_path = Path.home() / ".claude" / "projects"

        # Codex rollout logs root
        self.codex_sessions_path = Path(
            os.getenv(
                "CCBOT_CODEX_SESSIONS_PATH", str(Path.home() / ".codex" / "sessions")
            )
        )
        self.provider_data_root = (
            self.codex_sessions_path
            if self.provider == "codex"
            else self.claude_projects_path
        )
        self.provider_supports_hook = self.provider == "claude"

        self.monitor_poll_interval = float(os.getenv("MONITOR_POLL_INTERVAL", "2.0"))

        # Display user messages in history and real-time notifications
        # When True, user messages are shown with a 👤 prefix
        self.show_user_messages = True

        # Show hidden (dot) directories in directory browser
        self.show_hidden_dirs = (
            os.getenv("CCBOT_SHOW_HIDDEN_DIRS", "").lower() == "true"
        )

        # OpenAI API for voice message transcription (optional)
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )

        # Optional local port forwarding announcement on startup
        # Format: "3000" or "3000,5173"
        self.forward_ports: list[int] = []
        forward_ports_raw = os.getenv("CCBOT_FORWARD_PORTS", "").strip()
        if forward_ports_raw:
            for token in forward_ports_raw.split(","):
                part = token.strip()
                if not part:
                    continue
                try:
                    port = int(part)
                except ValueError as e:
                    raise ValueError(
                        f"CCBOT_FORWARD_PORTS contains non-numeric port: {part}"
                    ) from e
                if port < 1 or port > 65535:
                    raise ValueError(
                        f"CCBOT_FORWARD_PORTS contains invalid port: {port}"
                    )
                self.forward_ports.append(port)
        # Scrub sensitive vars from os.environ so child processes never inherit them.
        # Values are already captured in Config attributes above.
        for var in SENSITIVE_ENV_VARS:
            os.environ.pop(var, None)

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "provider=%s, tmux_session=%s, data_root=%s",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
            self.provider,
            self.tmux_session_name,
            self.provider_data_root,
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users


config = Config()
