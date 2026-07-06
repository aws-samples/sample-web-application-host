"""
Configuration loader for the Reserved Mode stacks.

Reuses the same pattern as the existing Autoscale stack (infrastructure/stack.py):
read config.ini from the repo root, with APP_* environment variables taking
precedence. Reserved-specific settings live under [Reserved*] sections so they
never collide with the Autoscale configuration.
"""
import configparser
import os
from pathlib import Path


# Repo root = infrastructure/reserved/config_loader.py -> up 3 levels.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_FILE = _REPO_ROOT / "config.ini"


class ReservedConfig:
    """Reads config.ini (env vars win) for Reserved Mode stacks."""

    def __init__(self) -> None:
        if not _CONFIG_FILE.exists():
            raise FileNotFoundError(
                f"config.ini not found at {_CONFIG_FILE}. "
                f"Copy config.ini.example to config.ini and edit it first."
            )
        self._config = configparser.ConfigParser()
        self._config.read(_CONFIG_FILE)

    def get(self, section: str, key: str, env_var: str = None, fallback: str = None) -> str:
        """Get a string value; environment variable takes precedence."""
        if env_var and os.getenv(env_var):
            return os.getenv(env_var)
        if fallback is not None:
            return self._config.get(section, key, fallback=fallback)
        return self._config.get(section, key)

    def get_int(self, section: str, key: str, env_var: str = None, fallback: int = None) -> int:
        value = self.get(section, key, env_var, None if fallback is None else str(fallback))
        return int(value)

    def get_tags(self) -> dict:
        """Return [Tags] as a dict (same convention as the Autoscale stack)."""
        if self._config.has_section("Tags"):
            return dict(self._config.items("Tags"))
        return {}
