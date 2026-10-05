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
