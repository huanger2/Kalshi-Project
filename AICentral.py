from ClaudeAi import ask_claude
from OpenAi import ask_openai
import pandas as pd


def get_ai_responses(question: str) -> pd.DataFrame:
    """
    Ask both Claude and OpenAI a question and return their responses in a dataframe.
    
    Args:
        question: Question to ask both AIs
    
    Returns:
        DataFrame with columns: AI, RESPONSE
    """
    # Get responses from both AIs
    claude_response = ask_claude(question)
    openai_response = ask_openai(question)
    
    # Create dataframe with responses
    df = pd.DataFrame({
        'AI': ['Claude', 'OpenAI'],
        'RESPONSE': [claude_response, openai_response]
    })
    
    return df
