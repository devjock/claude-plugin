#!/usr/bin/env python3
"""
Load the caller's DevJock system prompts into a file the initialize-devjock skill
reads at boot.

1. System prompts: every PLATFORM prompt whose context_window_position is
   preamble, then every one at postscript. The server returns only the prompts
   the caller's role may read (required_role_id, task 48-41401), so this script
   never decides who gets what. Sorted by sort_order, then id, as the CWO does.
2. The cloud skill and agent registry: name, description and id of every active
   platform agent template, so the session knows what it can invoke.
3. The session instructions, prompt 776, appended last.

Set DEVJOCK_ENV=proto to run against the proto stack (see skills/lib).
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Shown when the preamble fetch comes back empty. The server decides which platform
# prompts each role may see; a non-admin can legitimately get none, and without
# this notice the session would boot with no operating context and no warning.
NO_PROMPTS_NOTICE = (
    "DevJock returned no system prompts for your account's role. Your session "
    "will work, but without DevJock's operating guidance. If you expected them, "
    "ask your DevJock admin."
)

# --- Shared auth module (keychain/Credential-Manager-backed) ---
# Resolve the plugin root from THIS FILE'S OWN LOCATION, not from the
# CLAUDE_PLUGIN_ROOT env var: skill.md's `!`command`` line has ${CLAUDE_PLUGIN_ROOT}
# substituted into it textually by Claude Code (which is how this script itself
# gets found and run, regardless of what the local marketplace folder is named),
# but that substitution does NOT export CLAUDE_PLUGIN_ROOT as a real OS
# environment variable into this Python subprocess — os.environ.get() here
# returns None. __file__ is always accurate regardless of invocation path.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
_LIB = _PLUGIN_ROOT / "skills" / "lib"
if not _LIB.exists():
    # No fallback path here, on purpose. Guessing a hardcoded location fails
    # later and somewhere else ("file not found" in an unrelated import),
    # which is harder to debug than failing here with the actual reason.
    raise RuntimeError(
        f"Cannot resolve plugin root from __file__ (looked for {_LIB}); "
        "the script may have been invoked in an unsupported context."
    )
sys.path.insert(0, str(_LIB))
try:
    from authorize_devjock_api import get_access_token, API_BASE, DEVJOCK_ENV  # noqa: E402
except Exception:
    get_access_token = None
    API_BASE = DEVJOCK_ENV = None
API = API_BASE


def _category(p):
    c = p.get("category")
    return (c.get("name") if isinstance(c, dict) else c) or ""


def _get_json(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _first_list(data, *keys):
    """Return the first list found under the given keys, else the first list value."""
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return v
    for v in data.values():
        if isinstance(v, list):
            return v
    return []


# ---------- (1) system prompts ----------
# Old servers carry no context_window_position; map their prompt type instead
# (2 = preamble, 7 = postscript) so an admin is not handed every platform prompt.
_TYPE_POSITION = {2: "preamble", 7: "postscript"}


def _position(p):
    pos = p.get("context_window_position")
    if pos:
        return pos
    t = p.get("type")
    tid = t.get("id") if isinstance(t, dict) else (t if isinstance(t, int) else p.get("type_id"))
    return _TYPE_POSITION.get(int(tid)) if str(tid or "").isdigit() else None


def fetch_prompts(position, token):
    """Platform prompts at one context_window_position that the caller may read."""
    data = _get_json(f"{API}/prompts/list?scope=platform&context_window_position={position}"
                     "&resolves=prompt&page_size=300", token)
    items = [p for p in _first_list(data, "prompts")
             if p.get("active", True)
             and p.get("scope", "platform") == "platform"
             and _position(p) == position]
    items.sort(key=lambda p: (int(p.get("sort_order") or 0), int(p.get("id") or 0)))
    return items


def emit_prompts(items, header):
    out = [f"## {header}", ""]
    for p in items:
        pid = p.get("id")
        out.append(f"### [prompt {pid}: {p.get('name')}](https://www.devjock.ai/prompts/{pid})")
        out.append("")
        out.append((p.get("prompt") or "").rstrip())
        out.append("")
    return "\n".join(out)


# ---------- (2) skill + agent registry (agent-templates table) ----------
def fetch_templates(token):
    """All active PLATFORM agent templates, both category='skill' and 'agent'.

    The registry never blocks the prompts: on failure it is simply empty.
    """
    try:
        data = _get_json(f"{API}/agents/list?scope=platform&page_size=300", token)
    except Exception:
        return []
    items = [t for t in _first_list(data, "agents", "items")
             if t.get("active", True) and t.get("scope", "platform") == "platform"]
    # skills first (they are the invocable workflows), then agents; alpha within.
    items.sort(key=lambda t: (0 if _category(t).lower() == "skill" else 1,
                              (t.get("name") or "").lower()))
    return items


def fetch_description(agent_id, token):
    try:
        data = _get_json(f"{API}/agents/get?agent_id={agent_id}", token)
    except Exception:
        return ""
    if isinstance(data.get("description"), str):
        return data["description"]
    for k in ("agent", "data", "template"):
        v = data.get(k)
        if isinstance(v, dict) and isinstance(v.get("description"), str):
            return v["description"]
    return ""


def emit_registry(templates, token):
    out = [
        "## DevJock Skill & Agent Registry — the cloud template layer (live-injected)",
        "",
        ("These are DevJock **cloud** skills and agents (the `ai_agent_templates` table), "
         "NOT local Claude Code skills. Invoke via the `chat_with_agent` tool: pass "
         "`skill_ids=[id]` to layer a skill onto a chat, or `agent_id=id` (or the "
         "assistant's `task_id`) to talk to an agent. Descriptions below tell you what "
         "each does — read them before deciding which to use."),
        "",
    ]
    skills = [t for t in templates if _category(t).lower() == "skill"]
    agents = [t for t in templates if _category(t).lower() != "skill"]
    for group_name, group in (("Skills", skills), ("Agents", agents)):
        if not group:
            continue
        out.append(f"### {group_name}")
        out.append("")
        for t in group:
            tid = t.get("id")
            desc = " ".join((fetch_description(tid, token) or "").split()).strip()
            if not desc:
                desc = "(no description set)"
            out.append(f"- **{t.get('name')}** "
                       f"([agent {tid}](https://www.devjock.com/ai/agents/single/?id={tid})): {desc}")
        out.append("")
    return "\n".join(out), len(skills), len(agents)


INIT_PROMPT_ID = 776  # dj.initialize-devjock-workflow: the report template and rules, appended last


# The sign-in helper stores its token in the macOS Keychain or the Windows Credential Manager.
# Linux and other systems have neither, so the loader cannot sign in there. Instead of failing
# into a re-authenticate loop, send the model down the connector path the skill already has.
LINUX_NOTICE = (
    "*This computer is not macOS or Windows, so the DevJock loader cannot store a sign-in here. "
    "System prompts NOT injected by script. Use the DevJock connector instead: if it is not signed in, "
    "type /mcp, choose devjock and pick Authenticate; then call read_single_prompt(prompt_id=776) "
    "through the DevJock connector and follow it.*"
)


def main():
    if sys.platform not in ("darwin", "win32"):
        print(LINUX_NOTICE)
        return
    if get_access_token is None:
        print("*DevJock auth module unavailable; run /devjock:reauthenticate. System prompts NOT injected.*")
        return
    try:
        token = get_access_token()
        if not token:
            print("*DevJock token not found; run /devjock:reauthenticate to authenticate. System prompts NOT injected.*")
            return
        platform = fetch_prompts("preamble", token)
        post = fetch_prompts("postscript", token)
        templates = fetch_templates(token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("*DevJock sign-in expired (HTTP 401) — system prompts NOT injected. Run /devjock:reauthenticate.*")
        elif e.code == 400:
            # A server older than the role columns refuses a non-admin's platform
            # prompt list outright. That is "no prompts for this role", not a sign-in fault.
            print(f"**{NO_PROMPTS_NOTICE}** *(The DevJock server answered HTTP 400 to the system prompt request.)*")
        else:
            print(f"*DevJock API error HTTP {e.code} — system prompts NOT injected.*")
        return
    except Exception as e:
        print(f"*DevJock API error: {e} — system prompts NOT injected.*")
        return

    registry_md, n_skills, n_agents = emit_registry(templates, token)

    # The session instructions (report template, ledger rule, self-check) live in
    # DevJock as prompt 776, not in the plugin, so they are appended here and read
    # last. If the fetch fails the file still has the prompts; the skill says so.
    init_md = ""
    try:
        init = _get_json(f"{API}/prompts/get?prompt_id={INIT_PROMPT_ID}", token)
        init_body = (init.get("data") or init).get("prompt") if isinstance(init.get("data") or init, dict) else None
        if init_body:
            init_md = ("\n## Your session instructions — follow these now "
                       f"(dj.initialize-devjock-workflow, [prompt {INIT_PROMPT_ID}](https://www.devjock.ai/prompts/{INIT_PROMPT_ID}))\n\n" + init_body + "\n")
    except Exception as e:  # noqa: BLE001
        init_md = f"\n<!-- Session instructions (prompt {INIT_PROMPT_ID}) could not be fetched: {e} -->\n"

    header = (f"<!-- DevJock system prompts injected live: {len(platform)} system (preamble) "
              f"+ {len(post)} postscript prompts + {n_skills} skills + {n_agents} agents "
              f"(agent-templates registry). Order mirrors CWO. -->")
    if DEVJOCK_ENV != "production":
        header += f"\n<!-- DEVJOCK_ENV={DEVJOCK_ENV}: loaded from {API} -->"
    if not platform:
        header += f"\n<!-- {NO_PROMPTS_NOTICE} -->"

    platform_md = emit_prompts(platform, "DevJock System Prompts — your operating context (live-injected, preamble)")
    if not platform:
        platform_md += f"\n**{NO_PROMPTS_NOTICE}**\n"

    body = "\n".join([
        header,
        "",
        platform_md,
        emit_prompts(post, "DevJock Platform Postscripts (live-injected, postscript)"),
        registry_md,
        init_md,
    ])

    # --- Why we write a file instead of printing the bodies to stdout ---
    # The full system prompts is ~50k tokens. When a skill's `!`command`` inlines
    # that much stdout, the Claude Code harness SILENTLY truncates it to a ~2KB
    # preview and spills the rest to a persisted-output file. The agent then reads a
    # skill line claiming "these ARE your context" and proceeds having loaded almost
    # nothing. To make the load deterministic and un-fakeable, we ALWAYS write the
    # full context to a file and print only a compact pointer that stays well under
    # the cap — so nothing is ever silently dropped. The skill then forces a paged
    # Read of this file before it may emit its template.
    out_path = Path(__file__).resolve().parent / "injected-context.md"
    try:
        out_path.write_text(body, encoding="utf-8")
    except Exception as e:
        # Last-resort fallback: emit inline (may be truncated by the harness), but
        # say so loudly so the agent knows to distrust a preview.
        print("*Could not write injected-context file "
              f"({e}); emitting inline — THIS MAY BE TRUNCATED by the harness. "
              "If you see a persisted-output/preview wrapper, the context is INCOMPLETE.*")
        print()
        print(body)
        return

    approx_tokens = max(1, len(body) // 4)
    est_reads = approx_tokens // 24000 + 1
    print(header)
    print()
    if not platform:
        print(f"**{NO_PROMPTS_NOTICE}**")
        print()
    print("=============================================================================")
    print("  SYSTEM PROMPTS WAS WRITTEN TO A FILE — IT IS *NOT* INLINED IN THIS OUTPUT")
    print("=============================================================================")
    print()
    print(f"Full system prompts (~{approx_tokens:,} tokens) written to:")
    print(f"    {out_path}")
    print()
    print("Read that file with the Read tool to its LAST line, paging by offset (about")
    print(f"{est_reads} reads). It ends with your session instructions; follow them. Nothing in")
    print("it is in your context until you have read it. This block is only a pointer.")


if __name__ == "__main__":
    main()
