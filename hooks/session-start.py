#!/usr/bin/env python3
"""
SessionStart hook: initialize DevJock automatically when a Claude Code session starts.

What it does, in order:
  1. On a system without a credential store (not macOS or Windows), it injects one line
     pointing the model at the connector path, because the loader cannot sign in there.
  2. If no saved DevJock sign-in works, it injects one line saying the user should say
     "Initialize DevJock". It NEVER opens a browser itself: a login window popping up at
     every session start, unasked, would be worse than the step it saves.
  3. Otherwise it runs the existing loader (skills/initialize-devjock/inject-system-prompts.py)
     and injects its pointer output, so the session starts already initialized.
  4. After a compact it does not ask for the full reload. It injects the session's working
     context (task, latest handoff, agent sessions, memories, files, other sessions) inline,
     plus one line saying where the platform prompts are (task 48-41483).

It always exits 0 and prints valid hook JSON or nothing, so a DevJock problem can never
block a Claude Code session from starting.
"""
import contextlib
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "lib"))
sys.path.insert(0, str(ROOT / "skills" / "initialize-devjock"))


MANDATORY = (
    "MANDATORY FIRST STEP, BEFORE ANYTHING ELSE IN THIS SESSION. DevJock's system prompts were "
    "loaded at startup and are your operating context. Before you act on the user's first message, "
    "whatever it asks and however short a reply it wants, read the file named below with the Read "
    "tool, every page, to its very last line. Then reply with the DevJock startup report exactly as "
    "the session instructions at the end of that file specify, with every ledger row. Only after "
    "that report, in the same reply, do what the user asked. Reading part of the file, or skipping "
    "the report because the user asked for something else, is a failed initialization.\n\n"
)


def emit(text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": text,
    }}))


def saved_token_works():
    """True if a saved sign-in is valid or refreshes silently. Never opens a browser."""
    import authorize_devjock_api as auth
    saved = auth._load_tokens()
    if not saved:
        return False
    if auth.test_token(saved.get("access_token", "")):
        return True
    if "refresh_token" in saved and "client_id" in saved:
        try:
            return bool(auth.test_token(auth._refresh_token(saved["refresh_token"], saved["client_id"])))
        except Exception:
            return False
    return False


# The hook's injected text is capped at 10,000 characters; past that Claude Code keeps a
# 2,000-character preview and does not tell the model to read the rest.
HOOK_TEXT_LIMIT = 9000

AFTER_COMPACT = (
    "DevJock context after compaction. The working context below was reloaded from DevJock. "
    "DevJock's platform prompts are in the file named next; re-read it only if their "
    "rules are missing from the compaction summary. Do not repeat the DevJock startup report.\n\n"
)


def _hook_input():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _trim(text, limit=HOOK_TEXT_LIMIT):
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n\n…(trimmed; the full working context is in the file)"


def main():
    hook = _hook_input()
    source = hook.get("source") or ""
    loader_args = ["--no-inline",
                   "--session-id", str(hook.get("session_id") or ""),
                   "--data-dir", os.environ.get("CLAUDE_PLUGIN_DATA", ""),
                   "--transcript", str(hook.get("transcript_path") or "")]

    if sys.platform not in ("darwin", "win32"):
        emit("DevJock was not initialized automatically: this system has no credential store "
             "for the DevJock loader. When the user wants DevJock, sign in to the DevJock "
             "connector (/mcp, devjock, Authenticate), then call read_single_prompt(prompt_id=776) "
             "through it and follow it.")
        return

    try:
        ok = saved_token_works()
    except Exception:
        ok = False
    if not ok:
        emit("DevJock is installed but not signed in yet, so it was not initialized automatically. "
             "If the user wants to work with DevJock, ask them to say \"Initialize DevJock\"; "
             "that signs them in once and loads their DevJock context.")
        return

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "inject_system_prompts", ROOT / "skills" / "initialize-devjock" / "inject-system-prompts.py")
    loader = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(loader)
            result = loader.main(loader_args)
    except Exception as e:
        emit(f"DevJock automatic initialization failed ({e}). The user can say \"Initialize DevJock\" to retry.")
        return

    out = buf.getvalue()
    if "NOT injected" in out:
        # The loader signed in but could not load the prompts (auth expired, API error).
        emit("DevJock automatic initialization did not load the system prompts. The loader said:\n\n"
             + out + "\n\nTell the user, and suggest they say \"Initialize DevJock\" to retry.")
    elif "written to:" in out and source == "compact" and result:
        emit(_trim(AFTER_COMPACT + f"Platform prompts file: {result['path']}\n\n"
                   + result["working_context"]))
    elif "written to:" in out:
        emit(MANDATORY + out)
    else:
        # The loader could not write its file and printed the prompts inline instead.
        emit(MANDATORY.replace("read the file named below", "read the prompts below") + out)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
