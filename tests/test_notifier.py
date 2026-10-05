import smtplib
import unittest
from unittest import mock

import Notifier


class FakeError(Exception):
    pass


class IsInsufficientFundsErrorTests(unittest.TestCase):
    def test_openai_insufficient_quota_message(self):
        e = FakeError(
            "Error code: 429 - {'error': {'message': 'You exceeded your current quota, "
            "please check your plan and billing details.', 'type': 'insufficient_quota'}}"
        )
        self.assertTrue(Notifier.is_insufficient_funds_error(e))

    def test_anthropic_credit_balance_message(self):
        e = FakeError(
            "Your credit balance is too low to access the Anthropic API. Please go to "
            "Plans & Billing to upgrade or purchase credits."
        )
        self.assertTrue(Notifier.is_insufficient_funds_error(e))

    def test_gemini_resource_exhausted_message(self):
        e = FakeError("429 RESOURCE_EXHAUSTED. You exceeded your current quota.")
        self.assertTrue(Notifier.is_insufficient_funds_error(e))

    def test_gemini_billing_not_enabled_message(self):
        e = FakeError("400 FAILED_PRECONDITION. This API method requires billing to be enabled.")
        self.assertTrue(Notifier.is_insufficient_funds_error(e))

    def test_http_402_status_code_is_always_a_funds_error(self):
        e = FakeError("some vague wrapper message")
        e.status_code = 402
        self.assertTrue(Notifier.is_insufficient_funds_error(e))

    def test_code_attribute_is_also_checked(self):
        # google-genai's ClientError exposes the HTTP status as .code, not .status_code.
        e = FakeError("vague message")
        e.code = 402
        self.assertTrue(Notifier.is_insufficient_funds_error(e))

    def test_unrelated_errors_are_not_funds_errors(self):
        self.assertFalse(Notifier.is_insufficient_funds_error(FakeError("Connection timed out")))
        self.assertFalse(Notifier.is_insufficient_funds_error(ValueError("Could not parse response")))

    def test_plain_rate_limiting_without_quota_wording_is_not_a_funds_error(self):
        # A short-term 429 throttle shouldn't be mistaken for an empty wallet.
        e = FakeError("429 Too Many Requests - please slow down")
        self.assertFalse(Notifier.is_insufficient_funds_error(e))


class SendEmailTests(unittest.TestCase):
    def setUp(self):
        env_patch = mock.patch.dict(
            "os.environ",
            {
                "EMAIL_ADDRESS": "bot@example.com",
                "EMAIL_APP_PASSWORD": "secret",
                "ALERT_EMAIL_TO": "me@example.com",
                "SMTP_HOST": "smtp.example.com",
                "SMTP_PORT": "587",
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        # Never touch the real EmailAlerts.env file; use the patched env above.
        dotenv_patch = mock.patch("Notifier.load_dotenv")
        dotenv_patch.start()
        self.addCleanup(dotenv_patch.stop)

    def test_sends_via_smtp_with_starttls_and_login(self):
        smtp = mock.MagicMock()
        smtp.__enter__.return_value = smtp
        with mock.patch("smtplib.SMTP", return_value=smtp) as SMTP:
            ok = Notifier.send_email("subject", "body")

        self.assertTrue(ok)
        SMTP.assert_called_once_with("smtp.example.com", 587, timeout=30)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("bot@example.com", "secret")
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertEqual(sent_msg["To"], "me@example.com")
        self.assertEqual(sent_msg["From"], "bot@example.com")
        self.assertEqual(sent_msg["Subject"], "subject")

    def test_explicit_to_overrides_alert_email_to(self):
        smtp = mock.MagicMock()
        smtp.__enter__.return_value = smtp
        with mock.patch("smtplib.SMTP", return_value=smtp):
            Notifier.send_email("subject", "body", to="someone-else@example.com")
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertEqual(sent_msg["To"], "someone-else@example.com")

    def test_missing_credentials_fails_without_raising(self):
        with mock.patch.dict("os.environ", {"EMAIL_APP_PASSWORD": ""}):
            with mock.patch("smtplib.SMTP") as SMTP:
                ok = Notifier.send_email("subject", "body")
        self.assertFalse(ok)
        SMTP.assert_not_called()

    def test_smtp_failure_is_caught_and_returns_false_not_raised(self):
        with mock.patch("smtplib.SMTP", side_effect=smtplib.SMTPException("boom")):
            ok = Notifier.send_email("subject", "body")
        self.assertFalse(ok)


class NotifyInsufficientFundsTests(unittest.TestCase):
    def setUp(self):
        Notifier._notified.clear()
        self.addCleanup(Notifier._notified.clear)

    def test_sends_one_email_with_llm_name_in_subject_and_error_in_body(self):
        with mock.patch.object(Notifier, "send_email", return_value=True) as send:
            Notifier.notify_insufficient_funds("OpenAI", FakeError("no money"))
        send.assert_called_once()
        subject, body = send.call_args[0]
        self.assertIn("OpenAI", subject)
        self.assertIn("no money", body)

    def test_context_is_included_when_given(self):
        with mock.patch.object(Notifier, "send_email", return_value=True) as send:
            Notifier.notify_insufficient_funds("OpenAI", FakeError("no money"), context="Lakers win")
        self.assertIn("Lakers win", send.call_args[0][1])

    def test_is_deduplicated_per_llm_within_a_run(self):
        with mock.patch.object(Notifier, "send_email", return_value=True) as send:
            Notifier.notify_insufficient_funds("OpenAI", FakeError("no money"))
            Notifier.notify_insufficient_funds("OpenAI", FakeError("no money again"))
        send.assert_called_once()

    def test_different_llms_each_get_their_own_email(self):
        with mock.patch.object(Notifier, "send_email", return_value=True) as send:
            Notifier.notify_insufficient_funds("OpenAI", FakeError("no money"))
            Notifier.notify_insufficient_funds("Claude", FakeError("no money"))
        self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
