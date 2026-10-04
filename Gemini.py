from dotenv import load_dotenv
import os

load_dotenv('AIAPIKEYS.env')

_client = None


def _get_client():
    """Create the Gemini client on first use so importing this module never needs a key."""
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


def ask_gemini(question: str, max_tokens: int = 1024, model: str = "gemini-3.1-flash-lite") -> str:
    """Ask Gemini a question and return the response text."""
    response = _get_client().models.generate_content(
        model=model,
        contents=question,
        config={"max_output_tokens": max_tokens}
    )
    return response.text


if __name__ == "__main__":
    print(ask_gemini("What is the probability that the United States win the world cup? Also provide a precentage corresponding to how confident you are in that prediction. Answer in the form of'Probability: X%, Confidence: Y%'"))
