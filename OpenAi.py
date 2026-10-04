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
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def ask_openai(question: str, model: str | None = None) -> str:
    """Ask OpenAI a question and return the response text. Model defaults to ai_settings.json."""
    response = _get_client().chat.completions.create(
        model=model or get_model("openai"),
        messages=[
            {"role": "user", "content": question}
        ]
    )
    return response.choices[0].message.content
