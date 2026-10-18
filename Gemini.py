from dotenv import load_dotenv
import os

import CostTracker
from AISettings import get_model

# override=True: AIAPIKEYS.env must win over a stale OS-level env var of the
# same name, which load_dotenv() otherwise silently leaves in place.
load_dotenv('AIAPIKEYS.env', override=True)

_client = None


def _get_client():
    """Create the Gemini client on first use so importing this module never needs a key."""
    global _client
    if _client is None:
        from google import genai
        # .strip(): a key with trailing whitespace/newlines (a stray OS env var,
        # or a copy-paste artifact in the .env file) breaks requests outright.
        _client = genai.Client(api_key=(os.getenv("GEMINI_API_KEY") or "").strip())
    return _client


def ask_gemini(question: str, max_tokens: int = 1024, model: str | None = None) -> str:
    """Ask Gemini a question and return the response text. Model defaults to ai_settings.json."""
    model = model or get_model("gemini")
    response = _get_client().models.generate_content(
        model=model,
        contents=question,
        config={"max_output_tokens": max_tokens}
    )
    CostTracker.record_from_response(
        "Gemini", model, response, question,
        usage_attr="usage_metadata", input_field="prompt_token_count", output_field="candidates_token_count",
    )
    return response.text


def ask_gemini_with_search(question: str, max_tokens: int = 8192,
                           model: str | None = None) -> tuple[str, list[dict], list[str]]:
    """
    Like ask_gemini(), but Gemini may run Google searches to answer -- for
    questions about things newer than its training data. Returns the answer
    text, the web pages it drew on as [{"title", "uri"}], and the searches it
    ran. Gemini decides for itself whether to search: empty lists mean it
    answered from memory, which callers should treat as unverified.

    max_tokens is larger than ask_gemini()'s because searching and reasoning
    over the results spends output tokens before the answer itself. Google
    bills search grounding separately from tokens; that part isn't in the
    CostTracker estimate.
    """
    from google.genai import types

    model = model or get_model("gemini")
    response = _get_client().models.generate_content(
        model=model,
        contents=question,
        config=types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    CostTracker.record_from_response(
        "Gemini", model, response, "settings suggestions (with search)",
        usage_attr="usage_metadata", input_field="prompt_token_count", output_field="candidates_token_count",
    )
    sources, queries = [], []
    for candidate in response.candidates or []:
        metadata = getattr(candidate, "grounding_metadata", None)
        queries += [q for q in (getattr(metadata, "web_search_queries", None) or []) if q not in queries]
        for chunk in (getattr(metadata, "grounding_chunks", None) or []):
            web = getattr(chunk, "web", None)
            if web is not None and web.uri and all(s["uri"] != web.uri for s in sources):
                sources.append({"title": web.title or web.domain or web.uri, "uri": web.uri})
    return response.text or "", sources, queries
