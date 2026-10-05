from openai import OpenAI
from dotenv import load_dotenv
import os

import CostTracker
from AISettings import get_model

load_dotenv('AIAPIKEYS.env')

_client = None


def _get_client():
    """Create the client on first use so importing this module never needs a key."""
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def _record_usage(model: str, response, question: str) -> None:
    """Log token usage for a cost estimate. Swallows errors -- never breaks the actual call."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        CostTracker.record_usage(
            provider="OpenAI",
            model=model,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            context=question[:200],
        )
    except Exception as e:
        print(f"[OpenAi] failed to record usage: {e}")


def ask_openai(question: str, model: str | None = None) -> str:
    """Ask OpenAI a question and return the response text. Model defaults to ai_settings.json."""
    model = model or get_model("openai")
    response = _get_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "user", "content": question}
        ]
    )
    _record_usage(model, response, question)
    return response.choices[0].message.content
