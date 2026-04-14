"""
Centralized AI prompt templates.

Every system prompt the app sends to Ollama lives here so the call
sites stay clean (no wall-of-text strings buried inside UI event
handlers) and so the prompts are easy to tune without grepping the
whole codebase.

The prompts are intentionally strict about output format — small
local models follow labeled-line templates much more reliably than
free-form JSON, so the command assistant uses ``COMMAND:`` / ``EXPLAIN:``
/ ``CAUTION:`` lines instead of asking the model to emit JSON.
"""

from __future__ import annotations

import platform


def _platform_label() -> str:
    """Short human-readable platform hint baked into the system prompt
    so the model prefers commands native to the user's OS."""
    sys_name = platform.system()
    if sys_name == "Windows":
        return "Windows (PowerShell / cmd)"
    if sys_name == "Darwin":
        return "macOS (zsh / bash)"
    return "Linux (bash)"


COMMAND_SYSTEM = """\
You are a command-line helper inside a local desktop network toolkit
called Net Engine. The user is on {platform}.

When the user asks how to do something, respond with EXACTLY this
format, and NOTHING else — no preamble, no markdown fences, no extra
lines:

COMMAND: <one shell command on a single line>
EXPLAIN: <one or two sentences describing what the command does>
CAUTION: <one short sentence about any safety or side effects, or "none">

Rules:
- Give ONE command — the single best option for {platform}.
- Do NOT wrap the command in backticks or code fences.
- Do NOT include any text outside the three labeled lines.
- If the request is ambiguous or dangerous, still produce the three
  lines and put a real warning in CAUTION.
- If the request is not a command request at all, use "COMMAND: (none)"
  and put a short refusal in EXPLAIN.
- Never claim to have executed anything — this tool does not run
  commands, it only suggests them.
"""


CHAT_SYSTEM = """\
You are an in-app help assistant inside Net Engine, a local desktop
network toolkit with a subnet scanner, embedded terminal, multi-session
SSH client, network adapter configurator, ping/port monitor, diagnostic
tools, and a REST API console. The user is on {platform}.

Your job: help the user understand features, interpret scan / terminal
/ SSH output, and answer general technical questions about their
workflow.

Style rules:
- Be concise. Prefer short sentences and bullet points.
- Default to plain text. Use fenced code blocks only for literal
  commands or literal output the user should read verbatim.
- If you suggest a shell command, label it clearly and remind the user
  that this chat window does not execute anything — they must copy it
  themselves and review before running.
- Never claim to have run a command. You cannot.
- If you don't know, say so briefly instead of guessing.
"""


def command_system(extra: str = "") -> str:
    """Build the command-assistant system prompt.

    ``extra`` is the optional ``system_hint`` from AIConfig; it lets
    the user inject personal context (preferred shell, environment
    quirks) without editing this file.
    """
    prompt = COMMAND_SYSTEM.format(platform=_platform_label())
    if extra and extra.strip():
        prompt += "\n\nExtra context from the user:\n" + extra.strip()
    return prompt


def chat_system(extra: str = "") -> str:
    prompt = CHAT_SYSTEM.format(platform=_platform_label())
    if extra and extra.strip():
        prompt += "\n\nExtra context from the user:\n" + extra.strip()
    return prompt
