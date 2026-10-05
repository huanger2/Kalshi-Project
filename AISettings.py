"""
AISettings.py

Reads which model each AI API uses from ai_settings.json. The file is read on
every call, so editing it takes effect without restarting anything. Providers
missing from the file (or a missing file) fall back to DEFAULT_MODELS.
"""

import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).with_name("ai_settings.json")

DEFAULT_MODELS = {
    "claude": "claude-sonnet-5-5",
    "openai": "gpt-5.1",
    "gemini": "gemini-3.8-flash",
}


def get_model(provider: str) -> str:
    """Return the configured model for 'claude', 'openai' or 'gemini'."""
    if provider not in DEFAULT_MODELS:
        raise KeyError(f"Unknown AI provider {provider!r}; expected one of {sorted(DEFAULT_MODELS)}")

    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_MODELS[provider]
    except json.JSONDecodeError as e:
        # Fail loudly: silently ignoring a typo would run the wrong model.
        raise ValueError(f"{SETTINGS_PATH.name} is not valid JSON: {e}") from e

    return settings.get("models", {}).get(provider) or DEFAULT_MODELS[provider]
