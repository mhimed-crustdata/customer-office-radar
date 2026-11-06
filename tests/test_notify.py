import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402
from crustdata_api import load_env  # noqa: E402


class FakeMessages:
    def __init__(self, stop_reason="end_turn", text="Hello there."):
        self.kwargs = None
        self.stop_reason = stop_reason
        self.text = text

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=self.text)])


def fake_client(**kw):
    msgs = FakeMessages(**kw)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


class SlackTests(unittest.TestCase):
    def test_dry_run_never_sends(self):
        self.assertIn("dry-run", notify.send_slack("hi", dry_run=True, webhook_url="https://hooks.invalid/x"))

    def test_skips_without_webhook(self):
        old = os.environ.pop("SLACK_WEBHOOK_URL", None)
        try:
            self.assertIn("not set", notify.send_slack("hi"))
        finally:
            if old is not None:
                os.environ["SLACK_WEBHOOK_URL"] = old


class ClaudeTests(unittest.TestCase):
    def test_returns_text_only(self):
        client, msgs = fake_client()
        self.assertEqual(notify.ask_claude("x", client=client), "Hello there.")
        self.assertEqual(msgs.kwargs["model"], os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"))
        self.assertEqual(msgs.kwargs["output_config"], {"effort": "low"})

    def test_refusal_returns_none(self):
        client, _ = fake_client(stop_reason="refusal", text="")
        self.assertIsNone(notify.ask_claude("x", client=client))

    def test_unconfigured_returns_none(self):
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            self.assertIsNone(notify.ask_claude("x"))
        finally:
            if old is not None:
                os.environ["ANTHROPIC_API_KEY"] = old


class EnvTests(unittest.TestCase):
    def test_load_env_does_not_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write('# comment\nexport CD_TEST_A="one"\nCD_TEST_B=two\nCD_TEST_C=90   # days\nCD_TEST_D=    # empty\n')
        os.environ["CD_TEST_B"] = "kept"
        try:
            load_env(fh.name)
            self.assertEqual(os.environ["CD_TEST_A"], "one")
            self.assertEqual(os.environ["CD_TEST_B"], "kept")
            self.assertEqual(os.environ["CD_TEST_C"], "90")
            self.assertEqual(os.environ["CD_TEST_D"], "")
        finally:
            os.unlink(fh.name)
            os.environ.pop("CD_TEST_A", None)
            for k in ("CD_TEST_B", "CD_TEST_C", "CD_TEST_D"):
                os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()
