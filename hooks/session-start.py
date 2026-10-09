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

It always exits 0 and prints valid hook JSON or nothing, so a DevJock problem can never
block a Claude Code session from starting.
"""
import contextlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "lib"))
sys.path.insert(0, str(ROOT / "skills" / "initialize-devjock"))


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


def main():
    try:
        sys.stdin.read()  # hook input JSON; nothing in it is needed
    except Exception:
        pass

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
            loader.main()
    except Exception as e:
        emit(f"DevJock automatic initialization failed ({e}). The user can say \"Initialize DevJock\" to retry.")
        return

    emit("DevJock was initialized automatically when this session started. The loader output follows. "
         "Before your first reply, read the file it names to its last line and follow the session "
         "instructions at its end, exactly as the initialize-devjock skill would.\n\n" + buf.getvalue())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
