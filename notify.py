"""Optional integrations: Slack incoming webhook and Claude (Anthropic).

Both are off unless their env var is set, and both respect --dry-run.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional


def send_slack(text: str, dry_run: bool = False, webhook_url: Optional[str] = None) -> str:
    """Post `text` to a Slack incoming webhook. Returns what happened."""
    url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if dry_run:
        return "dry-run: Slack message not sent"
    if not url:
        return "SLACK_WEBHOOK_URL not set: Slack message skipped"
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    return "Slack message sent"


def claude_available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def ask_claude(prompt: str, max_tokens: int = 2000, client=None) -> Optional[str]:
    """One short Claude call. Returns None when Claude is not configured or declines.

    Needs `pip install anthropic` and ANTHROPIC_API_KEY. Model: ANTHROPIC_MODEL
    (default claude-opus-5-5). Uses low effort because the task is a short note.
    """
    if client is None:
        if not claude_available():
            return None
        import anthropic
        client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return None
    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
    return text.strip() or None
