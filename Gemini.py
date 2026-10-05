from dotenv import load_dotenv
import os

import CostTracker
from AISettings import get_model

load_dotenv('AIAPIKEYS.env')

_client = None


def _get_client():
    """Create the Gemini client on first use so importing this module never needs a key."""
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


def _record_usage(model: str, response, question: str) -> None:
    """Log token usage for a cost estimate. Swallows errors -- never breaks the actual call."""
    try:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        CostTracker.record_usage(
            provider="Gemini",
            model=model,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            context=question[:200],
        )
    except Exception as e:
        print(f"[Gemini] failed to record usage: {e}")


def ask_gemini(question: str, max_tokens: int = 1024, model: str | None = None) -> str:
    """Ask Gemini a question and return the response text. Model defaults to ai_settings.json."""
    model = model or get_model("gemini")
    response = _get_client().models.generate_content(
        model=model,
        contents=question,
        config={"max_output_tokens": max_tokens}
    )
    _record_usage(model, response, question)
    return response.text


if __name__ == "__main__":
    print(ask_gemini("What is the probability that the United States win the world cup? Also provide a precentage corresponding to how confident you are in that prediction. Answer in the form of'Probability: X%, Confidence: Y%'"))
