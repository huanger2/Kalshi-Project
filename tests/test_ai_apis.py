"""
Tests for the AI API wrappers (ClaudeAi, OpenAi, Gemini).

The unit tests mock every client, so they need no keys and cost nothing.

The live tests at the bottom call the real APIs. They are skipped unless
RUN_LIVE_AI_TESTS=1 is set, and each one is also skipped if its key is missing
from AIAPIKEYS.env / the environment:

    RUN_LIVE_AI_TESTS=1 python -m unittest tests.test_ai_apis -v
"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

import ClaudeAi
import Gemini
import OpenAi
from Probability import build_prompt, parse_response


def chat_completion(text):
    """Shape of an OpenAI-SDK chat.completions.create() response."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class ResetClientMixin:
    """Each module caches its client in a global; clear it around every test."""

    module = None

    def setUp(self):
        self.module._client = None
        self.addCleanup(setattr, self.module, "_client", None)


# ---------------------------------------------------------------------------
# Unit tests (mocked, no network)
# ---------------------------------------------------------------------------

class ClaudeAiTests(ResetClientMixin, unittest.TestCase):
    module = ClaudeAi

    def test_client_uses_anthropic_key_and_base_url(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
             mock.patch.object(ClaudeAi, "OpenAI") as openai_cls:
            ClaudeAi._get_client()
        openai_cls.assert_called_once_with(
            api_key="sk-ant-test", base_url="https://api.anthropic.com/v1"
        )

    def test_client_is_created_once_and_reused(self):
        with mock.patch.object(ClaudeAi, "OpenAI") as openai_cls:
            first = ClaudeAi._get_client()
            second = ClaudeAi._get_client()
        self.assertIs(first, second)
        openai_cls.assert_called_once()

    def test_ask_claude_sends_question_and_returns_text(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = chat_completion("Probability: 60%")
        with mock.patch.object(ClaudeAi, "_get_client", return_value=client),              mock.patch.object(ClaudeAi, "get_model", return_value="configured-model") as get_model:
            result = ClaudeAi.ask_claude("Will it rain?")

        self.assertEqual(result, "Probability: 60%")
        get_model.assert_called_once_with("claude")
        client.chat.completions.create.assert_called_once_with(
            model="configured-model",
            max_tokens=1024,
            messages=[{"role": "user", "content": "Will it rain?"}],
        )

    def test_ask_claude_passes_through_model_and_max_tokens(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = chat_completion("ok")
        with mock.patch.object(ClaudeAi, "_get_client", return_value=client):
            ClaudeAi.ask_claude("q", max_tokens=50, model="some-model")

        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "some-model")
        self.assertEqual(kwargs["max_tokens"], 50)

    def test_api_errors_propagate(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError("rate limited")
        with mock.patch.object(ClaudeAi, "_get_client", return_value=client):
            with self.assertRaises(RuntimeError):
                ClaudeAi.ask_claude("q")

    def test_usage_is_recorded_when_the_response_has_it(self):
        client = mock.Mock()
        usage = SimpleNamespace(prompt_tokens=11, completion_tokens=22)
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=usage
        )
        with mock.patch.object(ClaudeAi, "_get_client", return_value=client), \
             mock.patch.object(ClaudeAi, "get_model", return_value="configured-model"), \
             mock.patch("CostTracker.record_usage") as record:
            ClaudeAi.ask_claude("Will it rain?")
        record.assert_called_once_with(
            provider="Claude", model="configured-model", input_tokens=11, output_tokens=22,
            context="Will it rain?",
        )

    def test_missing_usage_does_not_record_anything(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = chat_completion("ok")  # no .usage attribute
        with mock.patch.object(ClaudeAi, "_get_client", return_value=client), \
             mock.patch("CostTracker.record_usage") as record:
            ClaudeAi.ask_claude("q")
        record.assert_not_called()

    def test_cost_tracker_failure_does_not_break_the_call(self):
        client = mock.Mock()
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=usage
        )
        with mock.patch.object(ClaudeAi, "_get_client", return_value=client), \
             mock.patch("CostTracker.record_usage", side_effect=RuntimeError("db locked")):
            result = ClaudeAi.ask_claude("q")
        self.assertEqual(result, "ok")


class OpenAiTests(ResetClientMixin, unittest.TestCase):
    module = OpenAi

    def test_client_uses_openai_key(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
             mock.patch.object(OpenAi, "OpenAI") as openai_cls:
            OpenAi._get_client()
        openai_cls.assert_called_once_with(api_key="sk-test")

    def test_client_is_created_once_and_reused(self):
        with mock.patch.object(OpenAi, "OpenAI") as openai_cls:
            self.assertIs(OpenAi._get_client(), OpenAi._get_client())
        openai_cls.assert_called_once()

    def test_ask_openai_sends_question_and_returns_text(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = chat_completion("Confidence: 40%")
        with mock.patch.object(OpenAi, "_get_client", return_value=client),              mock.patch.object(OpenAi, "get_model", return_value="configured-model") as get_model:
            result = OpenAi.ask_openai("Will it rain?")

        self.assertEqual(result, "Confidence: 40%")
        get_model.assert_called_once_with("openai")
        client.chat.completions.create.assert_called_once_with(
            model="configured-model",
            messages=[{"role": "user", "content": "Will it rain?"}],
        )

    def test_ask_openai_explicit_model_overrides_settings(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = chat_completion("ok")
        with mock.patch.object(OpenAi, "_get_client", return_value=client):
            OpenAi.ask_openai("q", model="some-model")

        self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], "some-model")

    def test_api_errors_propagate(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError("bad key")
        with mock.patch.object(OpenAi, "_get_client", return_value=client):
            with self.assertRaises(RuntimeError):
                OpenAi.ask_openai("q")

    def test_usage_is_recorded_when_the_response_has_it(self):
        client = mock.Mock()
        usage = SimpleNamespace(prompt_tokens=11, completion_tokens=22)
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=usage
        )
        with mock.patch.object(OpenAi, "_get_client", return_value=client), \
             mock.patch.object(OpenAi, "get_model", return_value="configured-model"), \
             mock.patch("CostTracker.record_usage") as record:
            OpenAi.ask_openai("Will it rain?")
        record.assert_called_once_with(
            provider="OpenAI", model="configured-model", input_tokens=11, output_tokens=22,
            context="Will it rain?",
        )

    def test_missing_usage_does_not_record_anything(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = chat_completion("ok")  # no .usage attribute
        with mock.patch.object(OpenAi, "_get_client", return_value=client), \
             mock.patch("CostTracker.record_usage") as record:
            OpenAi.ask_openai("q")
        record.assert_not_called()

    def test_cost_tracker_failure_does_not_break_the_call(self):
        client = mock.Mock()
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=usage
        )
        with mock.patch.object(OpenAi, "_get_client", return_value=client), \
             mock.patch("CostTracker.record_usage", side_effect=RuntimeError("db locked")):
            result = OpenAi.ask_openai("q")
        self.assertEqual(result, "ok")


class GeminiTests(ResetClientMixin, unittest.TestCase):
    module = Gemini

    def test_client_uses_gemini_key(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "gm-test"}), \
             mock.patch("google.genai.Client") as client_cls:
            Gemini._get_client()
        client_cls.assert_called_once_with(api_key="gm-test")

    def test_client_is_created_once_and_reused(self):
        with mock.patch("google.genai.Client") as client_cls:
            self.assertIs(Gemini._get_client(), Gemini._get_client())
        client_cls.assert_called_once()

    def test_ask_gemini_sends_question_and_returns_text(self):
        client = mock.Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="Probability: 10%")
        with mock.patch.object(Gemini, "_get_client", return_value=client),              mock.patch.object(Gemini, "get_model", return_value="configured-model") as get_model:
            result = Gemini.ask_gemini("Will it rain?")

        self.assertEqual(result, "Probability: 10%")
        get_model.assert_called_once_with("gemini")
        client.models.generate_content.assert_called_once_with(
            model="configured-model",
            contents="Will it rain?",
            config={"max_output_tokens": 1024},
        )

    def test_ask_gemini_passes_through_model_and_max_tokens(self):
        client = mock.Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="ok")
        with mock.patch.object(Gemini, "_get_client", return_value=client):
            Gemini.ask_gemini("q", max_tokens=50, model="some-model")

        kwargs = client.models.generate_content.call_args.kwargs
        self.assertEqual(kwargs["model"], "some-model")
        self.assertEqual(kwargs["config"], {"max_output_tokens": 50})

    def test_api_errors_propagate(self):
        client = mock.Mock()
        client.models.generate_content.side_effect = RuntimeError("quota")
        with mock.patch.object(Gemini, "_get_client", return_value=client):
            with self.assertRaises(RuntimeError):
                Gemini.ask_gemini("q")

    def test_usage_is_recorded_when_the_response_has_it(self):
        client = mock.Mock()
        usage = SimpleNamespace(prompt_token_count=5, candidates_token_count=7)
        client.models.generate_content.return_value = SimpleNamespace(text="ok", usage_metadata=usage)
        with mock.patch.object(Gemini, "_get_client", return_value=client), \
             mock.patch.object(Gemini, "get_model", return_value="configured-model"), \
             mock.patch("CostTracker.record_usage") as record:
            Gemini.ask_gemini("Will it rain?")
        record.assert_called_once_with(
            provider="Gemini", model="configured-model", input_tokens=5, output_tokens=7,
            context="Will it rain?",
        )

    def test_missing_usage_metadata_does_not_record_anything(self):
        client = mock.Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="ok")  # no usage_metadata
        with mock.patch.object(Gemini, "_get_client", return_value=client), \
             mock.patch("CostTracker.record_usage") as record:
            Gemini.ask_gemini("q")
        record.assert_not_called()

    def test_cost_tracker_failure_does_not_break_the_call(self):
        client = mock.Mock()
        usage = SimpleNamespace(prompt_token_count=1, candidates_token_count=1)
        client.models.generate_content.return_value = SimpleNamespace(text="ok", usage_metadata=usage)
        with mock.patch.object(Gemini, "_get_client", return_value=client), \
             mock.patch("CostTracker.record_usage", side_effect=RuntimeError("db locked")):
            result = Gemini.ask_gemini("q")
        self.assertEqual(result, "ok")


# ---------------------------------------------------------------------------
# Live tests (real API calls, opt-in)
# ---------------------------------------------------------------------------

LIVE = os.getenv("RUN_LIVE_AI_TESTS") == "1"

MARKET = {
    "title": "San Antonio wins",
    "rules_primary": "If San Antonio wins the Oklahoma City vs San Antonio game, "
                     "then the market resolves to Yes.",
}


def live_test(key_name):
    """Skip unless live tests are enabled and the provider's key is set."""
    if not LIVE:
        return unittest.skip("set RUN_LIVE_AI_TESTS=1 to call the real APIs")
    if not os.getenv(key_name):
        return unittest.skip(f"{key_name} not set in AIAPIKEYS.env")
    return lambda fn: fn


class LiveAPITests(unittest.TestCase):
    """
    One cheap 'are we connected' call per provider, plus the real contract the
    pipeline depends on: the reply to build_prompt() must parse.
    """

    def assert_answers(self, ask_fn):
        reply = ask_fn("Reply with exactly the word OK and nothing else.")
        self.assertIsInstance(reply, str)
        self.assertIn("OK", reply.upper())

    def assert_follows_prediction_format(self, ask_fn, name):
        reply = ask_fn(build_prompt(MARKET))
        prob, conf = parse_response(reply, name)  # raises if the format is wrong
        self.assertTrue(0.0 <= prob <= 1.0)
        self.assertTrue(0.0 <= conf <= 1.0)

    @live_test("ANTHROPIC_API_KEY")
    def test_claude_answers(self):
        self.assert_answers(ClaudeAi.ask_claude)

    @live_test("ANTHROPIC_API_KEY")
    def test_claude_follows_prediction_format(self):
        self.assert_follows_prediction_format(ClaudeAi.ask_claude, "Claude")

    @live_test("OPENAI_API_KEY")
    def test_openai_answers(self):
        self.assert_answers(OpenAi.ask_openai)

    @live_test("OPENAI_API_KEY")
    def test_openai_follows_prediction_format(self):
        self.assert_follows_prediction_format(OpenAi.ask_openai, "OpenAI")

    @live_test("GEMINI_API_KEY")
    def test_gemini_answers(self):
        self.assert_answers(Gemini.ask_gemini)

    @live_test("GEMINI_API_KEY")
    def test_gemini_follows_prediction_format(self):
        self.assert_follows_prediction_format(Gemini.ask_gemini, "Gemini")


if __name__ == "__main__":
    unittest.main()
