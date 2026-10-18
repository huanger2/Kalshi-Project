"""
webapp/suggestions.py

The Settings tab's "Ask Gemini for updates" button: Gemini looks up the
latest model of each AI, those models' prices, and announced league season
dates, and the answer becomes a list of proposed changes against the
current settings -- never applied automatically. An AI can confidently get
a model ID or a price wrong, and a bad model ID would break the next daily
run, so the admin reviews each change (with the sources Gemini cites) and
applies only the ones ticked, through the same validation as a manual edit
(webapp/settings.py).

Each part (models + pricing, league dates) is two calls, both parts in
parallel:

  1. Research: a short, plain "search the web for..." question, with Google
     Search enabled. Gemini decides for itself whether to search, and in
     practice it skips searching -- answering from possibly outdated memory --
     when the question also carries long output-format rules. Short, plain
     questions reliably make it search.
  2. Format: turning those notes into JSON, with no search, using only facts
     stated in the notes.

A part Gemini still answered without searching is flagged as unverified.
"""

import datetime
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from webapp import settings

RESEARCH_PROMPTS = {
    "models": """Search the web: as of {today}, what are the newest generally available (not preview) API models
from OpenAI, Anthropic (Claude) and Google (Gemini), with their exact API model IDs and standard API prices
in USD per 1 million input and output tokens? We currently use {current_models}; also give those models'
current prices, and say whether each has been succeeded by a newer model in the same tier.""",
    "leagues": """Search the web: as of {today}, what are the officially announced first day of the regular season and
first day of the postseason for each of these seasons? {seasons}. For the NBA, the postseason starts with
the play-in tournament. Say "not announced" for any date that isn't official yet.""",
}

FORMAT_PROMPTS = {
    "models": """Turn the research notes below into ONE JSON object (no other text), in exactly this shape:

{{
  "models": {{"openai": "<model ID>", "claude": "<model ID>", "gemini": "<model ID>"}},
  "pricing": {{"<model ID>": {{"input_per_million": <USD number or null>, "output_per_million": <USD number or null>}}}}
}}

- "models": for each provider, the exact API model ID of its newest generally available model in the same
  tier as the one currently used ({current_models}). If the notes don't clearly name a newer one, repeat the
  current ID. Never a preview, experimental or deprecated model.
- "pricing": standard prices in USD per 1,000,000 tokens for every model ID in "models" and every current
  model, exactly as the notes state them; null where the notes don't give one.

Use only facts stated in the notes. Do not add anything from your own knowledge.

Research notes:
{notes}""",
    "leagues": """Turn the research notes below into ONE JSON object (no other text), in exactly this shape:

{{
  "leagues": {{"<league>": {{"<season>": {{"regular_season_start": "YYYY-MM-DD" or null,
                                         "postseason_start": "YYYY-MM-DD" or null}}}}}}
}}

Seasons: {seasons}. Use only dates the notes state as official; null for anything not announced or not
mentioned. Do not add anything from your own knowledge.

Research notes:
{notes}""",
}

PARTS = [("models", "AI models and pricing"), ("leagues", "league dates")]


def _seasons_to_ask(today: datetime.date) -> dict[str, list[str]]:
    """Each league's current and next season -- the rows the Settings tab shows by default."""
    import Leagues
    out = {}
    for code, lg in Leagues.LEAGUES.items():
        start = lg.season_start_year(today)
        out[code] = [lg.season_label(start), lg.season_label(start + 1)]
    return out


def _prompt_values(view: dict, today: datetime.date) -> dict[str, str]:
    return {
        "today": today.isoformat(),
        "current_models": ", ".join(f"{p['name']} {p['model']}" for p in view["providers"]),
        "seasons": "; ".join(f"{code} {' and '.join(labels)}" for code, labels in _seasons_to_ask(today).items()),
    }


def build_research_prompts(view: dict, today: datetime.date) -> dict[str, str]:
    values = _prompt_values(view, today)
    return {key: RESEARCH_PROMPTS[key].format(**values) for key, _ in PARTS}


def build_format_prompt(key: str, notes: str, view: dict, today: datetime.date) -> str:
    return FORMAT_PROMPTS[key].format(notes=notes, **_prompt_values(view, today))


def parse_reply(text: str) -> dict:
    """The JSON object in Gemini's reply (with or without a ```json fence). ValueError if there isn't one."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    if not candidate:
        raise ValueError("Gemini's reply had no JSON in it.")
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ValueError(f"Gemini's reply wasn't valid JSON ({e}).") from None
    if not isinstance(data, dict):
        raise ValueError("Gemini's reply wasn't a JSON object.")
    return data


def _price(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _date(value) -> Optional[str]:
    try:
        return datetime.date.fromisoformat(value).isoformat() if isinstance(value, str) else None
    except ValueError:
        return None


def _money(value: Optional[float]) -> str:
    return "unknown" if value is None else f"${value:g}"


def changes(data: dict, view: dict, today: datetime.date) -> list[dict]:
    """
    The proposed changes: each {"id", "section", "label", "current", "suggested", "value"}, where
    "value" is what gets applied (as JSON) if ticked. Values that are malformed, unchanged, or about
    things the Settings tab doesn't manage are dropped.
    """
    out = []

    models = data.get("models") if isinstance(data.get("models"), dict) else {}
    current_models = {p["key"]: p for p in view["providers"]}
    for key, provider in current_models.items():
        suggested = models.get(key)
        if isinstance(suggested, str) and suggested.strip() and suggested.strip() != provider["model"]:
            suggested = suggested.strip()
            if len(suggested) <= 100 and not any(c.isspace() for c in suggested):
                out.append({"id": f"models|{key}", "section": "AI models", "label": provider["name"],
                            "current": provider["model"], "suggested": suggested, "value": json.dumps(suggested)})

    pricing = data.get("pricing") if isinstance(data.get("pricing"), dict) else {}
    relevant = {p["model"] for p in view["providers"]} | {
        c["suggested"] for c in out if c["id"].startswith("models|")}
    current_prices = {row["model"]: row for row in view["pricing"]}
    for model in sorted(relevant):
        entry = pricing.get(model)
        if not isinstance(entry, dict):
            continue
        new_in, new_out = _price(entry.get("input_per_million")), _price(entry.get("output_per_million"))
        if new_in is None and new_out is None:
            continue
        row = current_prices.get(model, {"input": None, "output": None})
        if (new_in, new_out) == (row["input"], row["output"]):
            continue
        out.append({"id": f"pricing|{model}", "section": "Model pricing", "label": model,
                    "current": "not listed" if model not in current_prices
                    else f"{_money(row['input'])} in / {_money(row['output'])} out",
                    "suggested": f"{_money(new_in)} in / {_money(new_out)} out",
                    "value": json.dumps([new_in, new_out])})

    leagues = data.get("leagues") if isinstance(data.get("leagues"), dict) else {}
    current_dates = {lg["code"]: {row["season"]: row for row in lg["seasons"]} for lg in view["leagues"]}
    for code, labels in _seasons_to_ask(today).items():
        by_season = leagues.get(code) if isinstance(leagues.get(code), dict) else {}
        for label in labels:
            entry = by_season.get(label) if isinstance(by_season.get(label), dict) else {}
            row = current_dates.get(code, {}).get(label, {})
            for field, key, name in (("regular", "regular_season_start", "regular season starts"),
                                     ("postseason", "postseason_start", "postseason starts")):
                suggested = _date(entry.get(key))
                if suggested is None or suggested == (row.get(field) or None):
                    continue
                out.append({"id": f"leagues|{code}|{label}|{field}", "section": "Leagues",
                            "label": f"{code} {label} {name}",
                            "current": row.get(field) or f"not set (default {row.get(field + '_default', '?')})",
                            "suggested": suggested, "value": json.dumps(suggested)})
    return out


def _default_research(prompt: str) -> tuple[str, list[dict], list[str]]:
    from Gemini import ask_gemini_with_search
    return ask_gemini_with_search(prompt)


def _default_format(prompt: str) -> str:
    from Gemini import ask_gemini
    # Room for Gemini's thinking tokens, which count against the same budget.
    return ask_gemini(prompt, max_tokens=8192)


def fetch(research: Optional[Callable[[str], tuple[str, list[dict], list[str]]]] = None,
          format_: Optional[Callable[[str], str]] = None,
          today: Optional[datetime.date] = None) -> dict:
    """
    Ask Gemini (both parts at once, in parallel) and return:

        {"changes": [...], "sources": [{"title", "uri"}], "asked_at": ISO time,
         "problems": [messages for parts that failed],
         "unsearched": [labels of parts Gemini answered without searching -- unverified]}

    `research` (search-enabled: prompt -> (notes, sources, queries)) and `format_` (prompt -> JSON
    text) default to the real Gemini calls; injectable so tests make no API calls. Raises ValueError
    (with a message for the admin) only if no part produced anything usable.
    """
    research = research or _default_research
    format_ = format_ or _default_format
    today = today or datetime.date.today()
    view = settings.view(today=today)
    prompts = build_research_prompts(view, today)

    def run_part(key: str) -> tuple[dict, list[dict], list[str]]:
        notes, sources, queries = research(prompts[key])
        if not notes.strip():
            raise ValueError("Gemini's research came back empty.")
        return parse_reply(format_(build_format_prompt(key, notes, view, today))), sources, queries

    with ThreadPoolExecutor(max_workers=len(PARTS)) as pool:
        futures = {key: pool.submit(run_part, key) for key, _ in PARTS}
    data, sources, problems, unsearched = {}, [], [], []
    for key, label in PARTS:
        try:
            part_data, part_sources, queries = futures[key].result()
        except Exception as e:
            problems.append(f"{label}: {e}")
            continue
        data.update(part_data)
        sources += [src for src in part_sources if src not in sources]
        if not part_sources and not queries:
            unsearched.append(label)
    if len(problems) == len(PARTS):
        raise ValueError("; ".join(problems))
    return {"changes": changes(data, view, today), "sources": sources,
            "asked_at": datetime.datetime.now().isoformat(timespec="minutes"),
            "problems": problems, "unsearched": unsearched}


def apply(form: dict, today: Optional[datetime.date] = None) -> tuple[list[str], list[str]]:
    """
    Apply the ticked changes (form fields apply::<id> plus value::<id>), section by section, through
    webapp/settings.py's validation. Returns (labels of sections saved, error messages); a section
    with an invalid value saves nothing, but doesn't stop the others.
    """
    today = today or datetime.date.today()
    chosen: dict[str, object] = {}
    errors: list[str] = []
    for key in form:
        if key.startswith("apply::"):
            change_id = key[len("apply::"):]
            try:
                chosen[change_id] = json.loads(form.get(f"value::{change_id}", ""))
            except (TypeError, json.JSONDecodeError):
                errors.append(f"{change_id}: the suggested value was unreadable; nothing applied for it.")

    view = settings.view(today=today)
    saved = []

    model_updates = {cid.split("|", 1)[1]: v for cid, v in chosen.items() if cid.startswith("models|")}
    if model_updates:
        form_models = {f"model::{p['key']}": str(model_updates.get(p["key"], p["model"])) for p in view["providers"]}
        try:
            settings.save_models(form_models)
            saved.append("AI models")
        except settings.SettingsError as e:
            errors += e.errors

    price_updates = {cid.split("|", 1)[1]: v for cid, v in chosen.items() if cid.startswith("pricing|")}
    if price_updates:
        try:
            settings.save_prices(price_updates)
            saved.append("model pricing")
        except settings.SettingsError as e:
            errors += e.errors

    date_updates = {tuple(cid.split("|")[1:]): v for cid, v in chosen.items() if cid.startswith("leagues|")}
    if date_updates:
        form_leagues = {}
        for lg in view["leagues"]:
            if lg["tracked"]:
                form_leagues[f"tracked::{lg['code']}"] = "on"
            for row in lg["seasons"]:
                for field in ("regular", "postseason"):
                    value = date_updates.get((lg["code"], row["season"], field), row[field])
                    form_leagues[f"{field}::{lg['code']}::{row['season']}"] = str(value or "")
        try:
            settings.save_leagues(form_leagues, today=today)
            saved.append("league dates")
        except settings.SettingsError as e:
            errors += e.errors
    return saved, errors
