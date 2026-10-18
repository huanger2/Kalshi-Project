"""
webapp/settings.py

The admin Settings tab: what each JSON config file currently says, and
validating + saving an edited section back through ConfigFiles (which
decides whether that edits the file itself or, hosted, a copy on the
volume -- see ConfigFiles.py).

    models   -> ai_settings.json    which model each AI uses
    weights  -> ai_weights.json     each AI's weight in the AI-odds consensus
    pricing  -> model_pricing.json  USD per 1M tokens, for the spend estimate
    leagues  -> leagues.json        season boundary dates (+ the tracking switches)

A section with any invalid field raises SettingsError and saves nothing.
Keys a section doesn't manage (e.g. "_comment") are carried over untouched.
"""

import datetime
import math
from pathlib import Path
from typing import Mapping, Optional

import ConfigFiles
import DataDir

PROVIDERS = [("openai", "OpenAI"), ("claude", "Claude"), ("gemini", "Gemini")]
MAX_WEIGHT = 10.0


class SettingsError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def paths() -> dict[str, Path]:
    """Each section's config file (the deployed copy; ConfigFiles resolves overrides)."""
    import AISettings
    import AIWeights
    import CostTracker
    import Leagues
    return {"models": AISettings.SETTINGS_PATH, "weights": AIWeights.WEIGHTS_PATH,
            "pricing": CostTracker.PRICING_PATH, "leagues": Leagues.CONFIG_PATH}


def _season_start_year(label: str) -> int:
    return int(label[:4])


def _league_seasons(code: str, configured: dict, today: datetime.date) -> list[str]:
    """Seasons to show for a league: every configured one, plus the current and next, newest first."""
    import Leagues
    lg = Leagues.get(code)
    current = lg.season_start_year(today)
    labels = set(configured) | {lg.season_label(current), lg.season_label(current + 1)}
    return sorted(labels, key=_season_start_year, reverse=True)


def view(today: Optional[datetime.date] = None) -> dict:
    """Everything the Settings page shows, read from the copies currently in effect."""
    import AISettings
    import AIWeights
    import Leagues

    today = today or datetime.date.today()
    files = paths()
    pricing = ConfigFiles.load(files["pricing"]).get("models", {})
    seasons_config = ConfigFiles.load(files["leagues"]).get("seasons", {})
    tracked = Leagues.enabled_leagues()

    leagues = []
    for code, lg in Leagues.LEAGUES.items():
        configured = seasons_config.get(code, {})
        rows = []
        for label in _league_seasons(code, configured, today):
            regular_default, post_default = lg.default_boundaries(_season_start_year(label))
            entry = configured.get(label) or {}
            rows.append({
                "season": label,
                "regular": entry.get("regular_season_start") or "",
                "postseason": entry.get("postseason_start") or "",
                "regular_default": regular_default.isoformat(),
                "postseason_default": post_default.isoformat(),
            })
        leagues.append({"code": code, "tracked": code in tracked, "seasons": rows})

    return {
        "hosted": bool(DataDir.data_dir()),
        "files": {section: {"name": path.name, "override": ConfigFiles.has_override(path)}
                  for section, path in files.items()},
        "providers": [{"key": key, "name": name, "model": AISettings.get_model(key),
                       "weight": AIWeights.get_weight(key)} for key, name in PROVIDERS],
        "pricing": [{"model": model, "input": (prices or {}).get("input_per_million"),
                     "output": (prices or {}).get("output_per_million")}
                    for model, prices in sorted(pricing.items())],
        "leagues": leagues,
    }


def _model_id(raw: str, label: str, errors: list[str]) -> Optional[str]:
    value = (raw or "").strip()
    if not value:
        errors.append(f"{label}: a model name is required.")
    elif len(value) > 100 or any(c.isspace() for c in value):
        errors.append(f"{label}: {value!r} doesn't look like a model name (no spaces, 100 characters max).")
    else:
        return value
    return None


def _number(raw: str, label: str, errors: list[str], minimum: float = 0.0,
            maximum: Optional[float] = None, required: bool = False) -> Optional[float]:
    value = (raw or "").strip()
    if not value:
        if required:
            errors.append(f"{label}: a number is required.")
        return None
    try:
        number = float(value)
    except ValueError:
        errors.append(f"{label}: {value!r} isn't a number.")
        return None
    if math.isnan(number) or math.isinf(number) or number < minimum or (maximum is not None and number > maximum):
        bounds = f"between {minimum:g} and {maximum:g}" if maximum is not None else f"{minimum:g} or more"
        errors.append(f"{label}: must be {bounds}.")
        return None
    return number


def _date(raw: str, label: str, errors: list[str]) -> Optional[datetime.date]:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        errors.append(f"{label}: {value!r} isn't a date like 2026-10-20.")
        return None


def save_models(form: Mapping[str, str]) -> None:
    errors: list[str] = []
    models = {key: _model_id(form.get(f"model::{key}", ""), name, errors) for key, name in PROVIDERS}
    if errors:
        raise SettingsError(errors)
    path = paths()["models"]
    config = ConfigFiles.load(path)
    config["models"] = models
    ConfigFiles.save(path, config)


def save_weights(form: Mapping[str, str]) -> None:
    errors: list[str] = []
    weights = {key: _number(form.get(f"weight::{key}", ""), f"{name} weight", errors,
                            maximum=MAX_WEIGHT, required=True) for key, name in PROVIDERS}
    if errors:
        raise SettingsError(errors)
    path = paths()["weights"]
    config = ConfigFiles.load(path)
    config["weights"] = weights
    ConfigFiles.save(path, config)


def save_pricing(form: Mapping[str, str]) -> None:
    errors: list[str] = []
    path = paths()["pricing"]
    config = ConfigFiles.load(path)
    current = config.get("models", {})

    models = {}
    for model in current:
        if form.get(f"delete::{model}") == "on":
            continue
        models[model] = {
            "input_per_million": _number(form.get(f"input::{model}", ""), f"{model} input price", errors),
            "output_per_million": _number(form.get(f"output::{model}", ""), f"{model} output price", errors),
        }
    new_name = (form.get("new_model") or "").strip()
    if new_name:
        name = _model_id(new_name, "New model", errors)
        if name in models:
            errors.append(f"New model: {name!r} is already listed.")
        elif name:
            models[name] = {
                "input_per_million": _number(form.get("new_input", ""), f"{name} input price", errors),
                "output_per_million": _number(form.get("new_output", ""), f"{name} output price", errors),
            }
    if errors:
        raise SettingsError(errors)
    config["models"] = dict(sorted(models.items()))
    ConfigFiles.save(path, config)


def save_leagues(form: Mapping[str, str], today: Optional[datetime.date] = None) -> None:
    import Leagues

    today = today or datetime.date.today()
    errors: list[str] = []
    path = paths()["leagues"]
    config = ConfigFiles.load(path)
    seasons_config = config.get("seasons", {})

    new_seasons: dict[str, dict] = {}
    for code in Leagues.LEAGUES:
        configured = seasons_config.get(code, {})
        league_seasons = {}
        for label in _league_seasons(code, configured, today):
            regular = _date(form.get(f"regular::{code}::{label}", ""), f"{code} {label} regular season start", errors)
            post = _date(form.get(f"postseason::{code}::{label}", ""), f"{code} {label} postseason start", errors)
            if regular and post and post <= regular:
                errors.append(f"{code} {label}: the postseason has to start after the regular season.")
            # Only seasons that were already in the file or now have a date
            # get written: the always-shown current/next season rows don't
            # pad the file with empty entries.
            if label in configured or regular or post:
                league_seasons[label] = {
                    "regular_season_start": regular.isoformat() if regular else None,
                    "postseason_start": post.isoformat() if post else None,
                }
        new_seasons[code] = dict(sorted(league_seasons.items(), key=lambda kv: _season_start_year(kv[0])))
    if errors:
        raise SettingsError(errors)

    config["seasons"] = new_seasons
    ConfigFiles.save(path, config)
    # Tracking goes through the same path as the league tabs' switches, so
    # it lands wherever they keep it (see Leagues.state_path()).
    tracked = set(Leagues.enabled_leagues())
    for code in Leagues.LEAGUES:
        want = form.get(f"tracked::{code}") == "on"
        if want != (code in tracked):
            Leagues.set_enabled(code, want)


SAVERS = {"models": save_models, "weights": save_weights, "pricing": save_pricing, "leagues": save_leagues}
