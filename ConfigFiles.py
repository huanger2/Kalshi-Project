"""
ConfigFiles.py

Which copy of a JSON config file (ai_settings.json, ai_weights.json,
model_pricing.json, leagues.json) is in effect, and saving edits to one --
what the webapp's Settings tab writes through.

Locally (DATA_DIR unset), there's one copy: the file in the project folder,
which a save edits directly.

Hosted (DATA_DIR set, see DataDir.py), the project folder is part of the
deployed code and is replaced on every deploy, so a save instead writes a
copy of the file into DATA_DIR (a persistent volume). That copy then
overrides the deployed one until it's reset -- so once a file has been
saved from the Settings tab on the server, edits to it pushed from git
don't take effect there until "Reset to repo version" deletes the copy.
"""

import json
from pathlib import Path
from typing import Optional

import DataDir


def _override_path(repo_path: Path) -> Optional[Path]:
    base = DataDir.data_dir()
    return Path(base) / repo_path.name if base else None


def effective(repo_path: Path) -> Path:
    """The copy to read: a saved override on the volume if there is one, else the deployed file."""
    override = _override_path(repo_path)
    return override if override is not None and override.exists() else repo_path


def has_override(repo_path: Path) -> bool:
    override = _override_path(repo_path)
    return override is not None and override.exists()


def save_target(repo_path: Path) -> Path:
    """Where a save goes: the volume copy when hosted, else the file itself."""
    return _override_path(repo_path) or repo_path


def load(repo_path: Path) -> dict:
    """The effective copy's contents ({} if there's no file at all)."""
    path = effective(repo_path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ValueError(f"{path.name} is not valid JSON: {e}") from e


def dumps(obj, _depth: int = 0) -> str:
    """
    JSON laid out the way these files are written by hand: the top two
    levels of objects one key per line, deeper objects without nested
    objects inside (a model's prices, a season's dates) on a single line.
    """
    expand = isinstance(obj, dict) and obj and (_depth < 2 or any(isinstance(v, dict) for v in obj.values()))
    if not expand:
        return json.dumps(obj)
    pad = " " * 4 * (_depth + 1)
    items = [f"{pad}{json.dumps(key)}: {dumps(value, _depth + 1)}" for key, value in obj.items()]
    return "{\n" + ",\n".join(items) + "\n" + " " * 4 * _depth + "}"


def save(repo_path: Path, config: dict) -> Path:
    """Write `config` to save_target() atomically (write, then rename). Returns where it went."""
    target = save_target(repo_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(dumps(config) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def reset(repo_path: Path) -> bool:
    """Delete the volume override, so the deployed file is in effect again. False if there wasn't one."""
    override = _override_path(repo_path)
    if override is None or not override.exists():
        return False
    override.unlink()
    return True
