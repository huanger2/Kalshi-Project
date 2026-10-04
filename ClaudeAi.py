from openai import OpenAI
from dotenv import load_dotenv
import os

from AISettings import get_model

load_dotenv('AIAPIKEYS.env')

_client = None


def _get_client():
    """Create the client on first use so importing this module never needs a key."""
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            base_url="https://api.anthropic.com/v1"
        )
    return _client


def ask_claude(question: str, max_tokens: int = 1024, model: str | None = None) -> str:
    """Ask Claude a question and return the response text. Model defaults to ai_settings.json."""
    response = _get_client().chat.completions.create(
        model=model or get_model("claude"),
        max_tokens=max_tokens,
        messages=[
            {"role": "user", "content": question}
        ]
    )
    return response.choices[0].message.content
