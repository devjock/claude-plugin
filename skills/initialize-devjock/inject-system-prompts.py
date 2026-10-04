#!/usr/bin/env python3
"""
Inject DevJock system prompts into the initialize-devjock skill at boot — the
EA3 pattern applied to the plugin. Two kinds of context are injected:

1. System PROMPTS + POSTSCRIPTS (prompts table, type_id=2 then 7) — the live
   operating context. Order mirrors the live CWO (ChatService.php). Active only;
   category='skill' prompts skipped (legacy: skills used to be prompts); sort ASC.

2. The SKILL + AGENT REGISTRY (ai_agent_templates table, BOTH category='skill'
   and category='agent' — same table, they just behave differently). A registry
   of name + DESCRIPTION + id, NOT full bodies, so a Claude Code session knows
   what cloud skills/agents exist and can invoke them (via chat_with_agent:
   skill_ids=[id] to layer a skill, or agent_id / the assistant's task).

WHY (2) EXISTS — the fix shipped 2026-07-07: skills were migrated from prompts to
agent-templates (so they could carry tools). The old injector only read the
prompts table, so it NEVER saw skills like /manage-task — every Claude Code
session booted blind to the skill layer. This now loads the agent-templates
table directly.

Workspace/agent prompts (type_id=3) are intentionally NOT injected — a Claude Code
session is not workspace- or agent-scoped by default. The registry is scoped to
PLATFORM templates (workspace_id=-2), which is what a session should know about.

NOTE (perf/follow-up): descriptions are fetched per-template via /agents/get
because /agents/list omits the description column (same omission as /tools/list —
see bug 48-40815). Platform template count is small (~20), so ~20 boot calls is
acceptable; collapse to one call once the list endpoint returns description.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

API = "https://api.devjock.com/v1.0"
PLATFORM_WS = -2

# Shown when the type-2 fetch comes back empty. The server decides which platform
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
    from authorize_devjock_api import get_access_token  # noqa: E402
except Exception:
    get_access_token = None


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


# ---------- (1) system prompts + postscripts ----------
# The API only lets a PLATFORM ADMIN list prompts without naming a workspace; any
# other caller gets 400 "workspace_id is required". Platform-scoped prompts are
# visible to every caller whatever workspace they name, so a regular workspace
# member names one workspace they belong to and keeps only scope=platform rows.
def member_workspace_id(token):
    """First workspace the caller belongs to, or None if the lookup fails."""
    try:
        data = _get_json(f"{API}/workspaces/list?page_size=1", token)
        ws = _first_list(data, "workspaces")
        return ws[0].get("id") if ws else None
    except Exception:
        return None


def fetch_prompts(type_id, token, workspace_id=None):
    base = f"{API}/prompts/list?type_id={type_id}&resolves=prompt&page_size=300"
    urls = [f"{base}&workspace_id={workspace_id}"] if workspace_id is not None else []
    urls.append(base)  # platform admins may omit workspace_id
    last_err = None
    for url in urls:
        try:
            data = _get_json(url, token)
            break
        except urllib.error.HTTPError as e:
            last_err = e
    else:
        raise last_err
    # The server is authoritative for what a caller may see; this filter only tidies, it does not hide anything.
    items = [p for p in _first_list(data, "prompts")
             if p.get("active", True) and _category(p).lower() != "skill"
             and p.get("scope", "platform") == "platform"]
    items.sort(key=lambda p: (p.get("sort_order") or 0, p.get("id") or 0))
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
    """All active PLATFORM agent-templates — both category='skill' and 'agent'.

    scope=platform is the current marker; workspace_id=-2 is the retired one that
    older servers still use. Try both and keep platform-scoped rows. The registry
    is never allowed to block the prompts: on total failure it is simply empty.
    """
    for q in ("scope=platform", f"workspace_id={PLATFORM_WS}"):
        try:
            data = _get_json(f"{API}/agents/list?{q}&page_size=300", token)
        except Exception:
            continue
        items = [t for t in _first_list(data, "agents", "items")
                 if t.get("active", True) and t.get("scope", "platform") == "platform"]
        if items:
            break
    else:
        items = []
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


# ---------- role preview (platform admins dogfooding the customer view) ----------
# DEVJOCK_ROLE_PREVIEW=workspace-admin makes a platform admin's boot load only what a
# workspace admin/member gets: the prompts grouped in the user prompt agent (agent 190,
# the bundle chat loads for non-admins). Client-side and for preview ONLY — the server
# remains the authority on what a real non-admin can read.
INIT_PROMPT_ID = 776  # dj.initialize-devjock-workflow: the report template and rules, appended last
USER_PROMPT_AGENT_ID = 190            # /load-user-prompts  (workspace admin)
WORKSPACE_USER_PROMPT_AGENT_ID = 477  # /load-workspace-user-prompts (workspace user)


def role_preview():
    """DEVJOCK_ROLE_PREVIEW = none | workspace-user | workspace-admin | platform-admin.

    Mirrors what chat delivers per role (task 48-40386): platform admins get every
    system prompt; workspace admins get /load-user-prompts (agent 190), workspace
    users get /load-workspace-user-prompts (agent 477), both plus the postscript; a
    caller with no workspace role gets none.
    """
    v = (os.environ.get("DEVJOCK_ROLE_PREVIEW") or "").strip().lower().replace("_", "-")
    aliases = {"user": "workspace-user", "member": "workspace-user", "workspace-member": "workspace-user", "admin": "workspace-admin"}
    v = aliases.get(v, v)
    return v if v in ("none", "workspace-user", "workspace-admin", "platform-admin") else None


def user_agent_prompt_ids(token, agent_id=USER_PROMPT_AGENT_ID):
    data = _get_json(f"{API}/agents/get?agent_id={agent_id}", token)
    for src in (data, data.get("agent") or {}, data.get("data") or {}):
        ids = src.get("prompt_ids") if isinstance(src, dict) else None
        if isinstance(ids, list):
            return {int(i) for i in ids}
    raise RuntimeError(f"agent {agent_id} returned no prompt_ids")


def main():
    if get_access_token is None:
        print("*DevJock auth module unavailable; run /devjock:reauthenticate. System prompts NOT injected.*")
        return
    try:
        token = get_access_token()
        if not token:
            print("*DevJock token not found; run /devjock:reauthenticate to authenticate. System prompts NOT injected.*")
            return
        ws = member_workspace_id(token)
        platform = fetch_prompts(2, token, ws)
        post = fetch_prompts(7, token, ws)
        templates = fetch_templates(token)
        preview = role_preview()
        if preview:
            full_count = len(platform) + len(post)
            if preview == "none":
                platform, post = [], []
            elif preview in ("workspace-user", "workspace-admin"):
                agent_id = WORKSPACE_USER_PROMPT_AGENT_ID if preview == "workspace-user" else USER_PROMPT_AGENT_ID
                allowed = user_agent_prompt_ids(token, agent_id)
                platform = [p for p in platform if int(p.get("id") or 0) in allowed]
                # postscript (type 7) is loaded for every role by chat, so it stays
    except urllib.error.HTTPError as e:
        print(f"*DevJock API error HTTP {e.code} — system prompts NOT injected. Run /devjock:reauthenticate if token expired.*")
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

    header = (f"<!-- DevJock system prompts injected live: {len(platform)} system (type 2) "
              f"+ {len(post)} postscript (type 7) prompts + {n_skills} skills + {n_agents} agents "
              f"(agent-templates registry). Order mirrors CWO. -->")
    if preview:
        header += (f"\n<!-- ROLE PREVIEW: {preview} — showing {len(platform) + len(post)} of "
                   f"{full_count} system prompts. "
                   f"Unset DEVJOCK_ROLE_PREVIEW for the full admin context. -->")
    if not platform:
        header += f"\n<!-- {NO_PROMPTS_NOTICE} -->"

    platform_md = emit_prompts(platform, "DevJock System Prompts — your operating context (live-injected, type 2)")
    if not platform:
        platform_md += f"\n**{NO_PROMPTS_NOTICE}**\n"

    body = "\n".join([
        header,
        "",
        platform_md,
        emit_prompts(post, "DevJock Platform Postscripts (live-injected, type 7)"),
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
