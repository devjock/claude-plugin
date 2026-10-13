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
3. The session's working context (task 48-41483): when the session's name follows
   devjock_{task_id}_{YYYY-MM-DD}_{agent-type}_{task-name}, links to that task, its
   latest HANDOFF blog post, its agent sessions, memories and files; and, for any
   session, the other Claude Code sessions running on this machine. Pointers only.
4. The session instructions, prompt 776, appended last.

The file is written per session, to {data dir}/context/{session id}.md, so two sessions
never read each other's task. Callers pass --session-id and --data-dir; the skill gets
both from Claude Code's text substitution, the SessionStart hook from its stdin and
environment. Without both, the file goes to the old shared path and carries no task.

Set DEVJOCK_ENV=proto to run against the proto stack (see skills/lib).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
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


def _get_json(url, token, timeout=20, method="GET", body=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        "## DevJock Agent Blueprints (live-injected)",
        "",
        ("These are DevJock **cloud** agent blueprints (the `ai_agent_templates` table), "
         "NOT local Claude Code skills. A blueprint is configuration (model, prompts, "
         "tools) and does nothing on its own: it runs inside an agent session. Use them "
         "through the `chat_with_agent` tool: pass `skill_ids=[id]` to layer a skill "
         "blueprint onto an agent session, or `agent_id=id` to start an agent session "
         "that runs on that blueprint. To talk to a specific assistant, pass its "
         "`task_id` instead. Descriptions below say what each blueprint configures; "
         "read them before choosing."),
        "",
    ]
    skills = [t for t in templates if _category(t).lower() == "skill"]
    agents = [t for t in templates if _category(t).lower() != "skill"]
    for group_name, group in (("Skill blueprints", skills), ("Other agent blueprints", agents)):
        if not group:
            continue
        out.append(f"### {group_name}")
        out.append("")
        for t in group:
            tid = t.get("id")
            desc = " ".join((fetch_description(tid, token) or "").split()).strip()
            if not desc:
                desc = "(no description set)"
            out.append(f"- [{t.get('name')} (a{tid})](https://www.devjock.ai/agents/{tid}): {desc}")
        out.append("")
    return "\n".join(out), len(skills), len(agents)


# ---------- (3) the session's working context (task 48-41483) ----------
SESSION_NAME_RE = re.compile(r"^devjock_(\d+-\d+)_(\d{4}-\d{2}-\d{2})_([a-z0-9-]+)_(.+)$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CONTEXT_BUDGET_SECONDS = 30   # all working-context reads together
CONTEXT_CALL_TIMEOUT = 10     # any one of them
MAX_AGENT_SESSIONS = 10
CONTEXT_MAX_AGE_DAYS = 7
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
WEB = "https://www.devjock.ai"


def _valid_session_id(value):
    return bool(value) and bool(UUID_RE.match(value.strip().lower()))


def _valid_data_dir(value):
    # An unsubstituted ${CLAUDE_PLUGIN_DATA} reaches bash as an empty string; the
    # literal check is a backstop for any caller that does not go through a shell.
    return bool(value) and "${" not in value and Path(value).is_absolute()


def _registry_sessions():
    """Claude Code's live-session registry, ~/.claude/sessions/<pid>.json. Undocumented;
    any problem reading it means no entries, never an error."""
    out = []
    try:
        files = list(SESSIONS_DIR.glob("*.json"))
    except Exception:
        return out
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("sessionId"):
            out.append(d)
    return out


def _pid_alive(pid):
    if sys.platform == "win32":
        # os.kill(pid, 0) would terminate the process on Windows; ask the kernel instead.
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def session_name(session_id, transcript_path=None, registry=None):
    """This session's name: the live-session registry first, then the transcript's
    last custom-title entry. None when neither has one."""
    # A name Claude Code made up ("derived", e.g. on a headless `claude -p --resume`) is not
    # the user's; skip it so the transcript's custom title is used instead.
    entries = [d for d in (registry if registry is not None else _registry_sessions())
               if d.get("sessionId") == session_id and d.get("name")
               and d.get("nameSource") != "derived"]
    # A crashed run can leave a stale file under the same session id; the live one wins.
    entries.sort(key=lambda d: not _pid_alive(d.get("pid")))
    if entries:
        return entries[0]["name"]
    if transcript_path:
        title = None
        try:
            with open(transcript_path, encoding="utf-8") as fh:
                for line in fh:
                    if '"custom-title"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if entry.get("type") == "custom-title" and entry.get("customTitle"):
                        title = entry["customTitle"]
        except Exception:
            pass
        return title
    return None


def parse_session_name(name):
    """(task_id, date, agent_type, task_name) for a DevJock session name, else None."""
    m = SESSION_NAME_RE.match(name or "")
    return m.groups() if m else None


def _as_list(value):
    """The API returns some collections as a list and some as an object keyed "1", "2", …"""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [v for _, v in sorted(value.items(), key=lambda kv: str(kv[0]).zfill(6)) if isinstance(v, dict)]
    return []


def _one_line(text, limit=160):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def timeout(self):
        left = self.deadline - time.monotonic()
        if left <= 0.5:
            raise TimeoutError("working-context time budget used up")
        return min(CONTEXT_CALL_TIMEOUT, left)


def _section(title, build):
    """Run one section builder; a failure becomes a 'could not load' line, never an error."""
    try:
        lines = build()
    except TimeoutError:
        lines = ["- not loaded (timeout)"]
    except Exception as e:  # noqa: BLE001
        lines = [f"- could not load ({type(e).__name__})"]
    return [f"### {title}", ""] + (lines or ["- none"]) + [""]


def _task_sections(task_id, token, templates, budget):
    q = urllib.parse.quote(task_id)
    task = {}

    def task_block():
        task.update(_get_json(f"{API}/tasks/single?task_id={q}", token, timeout=budget.timeout(),
                              method="POST",
                              body={"resolves": ["title", "ai_summary", "attached_files", "memory_meta"]}))
        lines = [f"- [{task.get('title') or task_id} ({task_id})]({WEB}/tasks/{task_id})"]
        summary = (task.get("ai_summary") or "").strip().split("\n\n")[0]
        if summary:
            lines.append(f"- Summary: {_one_line(summary, 400)}")
        return lines

    def handoff_block():
        data = _get_json(f"{API}/blogs/list?task_id={q}&search_term=HANDOFF&page_size=5",
                         token, timeout=budget.timeout())
        for b in _first_list(data, "blog_posts"):
            excerpt = (b.get("excerpt") or "").lstrip("*# ").strip()
            if excerpt.startswith("HANDOFF —"):
                title = _one_line(excerpt.split("\n")[0], 120)
                return [f"- [{title} ({task_id})]({WEB}/tasks/{task_id}#{b.get('blog_id')}) — "
                        "the latest handoff; read it before starting work"]
        return ["- no handoff found"]

    names = {t.get("id"): t.get("name") for t in templates}

    def fetch_name(aid):
        d = _get_json(f"{API}/agents/get?agent_id={aid}", token, timeout=budget.timeout())
        for v in (d, d.get("data"), d.get("agent"), d.get("template")):
            if isinstance(v, dict) and v.get("name"):
                return v["name"]
        return None

    def sessions_block():
        data = _get_json(f"{API}/chatsessions/list?task_id={q}&resolves=ai_summary&page_size=25",
                         token, timeout=budget.timeout())
        rows = [c for c in _first_list(data, "chat_sessions") if not c.get("is_archived")][:MAX_AGENT_SESSIONS]
        lines = []
        for c in rows:
            agent = ""
            if time.monotonic() > budget.deadline - 0.5:
                # Keep the sessions already listed; say the rest were cut off.
                lines.append(f"- … {len(rows) - len(lines)} more not loaded (timeout)")
                break
            try:
                g = _get_json(f"{API}/chatsessions/get?chat_id={c.get('id')}", token, timeout=budget.timeout())
                g = g.get("data", g) if isinstance(g, dict) else {}
                aid = g.get("agent_id")
                if aid:
                    if aid not in names:
                        # Not a platform template; one read each, cached for the run.
                        try:
                            names[aid] = fetch_name(aid)
                        except TimeoutError:
                            raise
                        except Exception:
                            names[aid] = None
                    agent = f" — {names.get(aid) or f'agent {aid}'}"
            except Exception:
                pass  # a timeout here only loses this row's blueprint name
            summary = _one_line(c.get("ai_summary"), 160)
            lines.append(f"- [{_one_line(c.get('chat_name') or 'untitled', 90)} (c{c.get('id')})]"
                         f"({WEB}/chat/{c.get('id')}){agent}" + (f": {summary}" if summary else ""))
        return lines

    def memories_block():
        return [f"- [{m.get('name')} (m{m.get('id')})]({WEB}/memory/{m.get('id')})"
                for m in _as_list(task.get("memory_meta")) if m.get("id")]

    def files_block():
        out = []
        for f in _as_list(task.get("attached_files")):
            name = f.get("file_display_name") or f.get("file_name") or f.get("name") or "file"
            summary = _one_line(f.get("ai_summary"), 120)
            out.append(f"- {name} (file {f.get('id') or f.get('file_id')})" + (f": {summary}" if summary else ""))
        return out

    lines = _section("Task", task_block)
    lines += _section("Latest handoff", handoff_block)
    lines += _section("Agent sessions on this task", sessions_block)
    lines += _section("Memories", memories_block if task else (lambda: ["- not loaded (task read failed)"]))
    lines += _section("Files", files_block if task else (lambda: ["- not loaded (task read failed)"]))
    return lines


def _other_sessions_lines(session_id, registry):
    lines = []
    for d in registry:
        if d.get("sessionId") == session_id or not _pid_alive(d.get("pid")):
            continue
        sock = d.get("messagingSocketPath")
        addr = f" — message with SendMessage to `uds:{sock}`" if sock else ""
        lines.append(f"- {d.get('name') or d.get('sessionId')} ({d.get('status') or 'unknown'}){addr}")
    return lines


def working_context(session_id, transcript_path, token, templates, include_task=True):
    """Markdown for the session's working context. Pointers and one line each."""
    registry = _registry_sessions()
    out = ["## Your working context — this session's task (live-injected)", ""]
    name = session_name(session_id, transcript_path, registry) if session_id else None
    parsed = parse_session_name(name) if include_task else None
    if parsed:
        task_id, _date, agent_type, _slug = parsed
        out += [f"Session `{name}`: task {task_id}, working as {agent_type}.", ""]
        out += _task_sections(task_id, token, templates, _Budget(CONTEXT_BUDGET_SECONDS))
    elif not include_task:
        out += ["No task context: this session's id or the plugin data folder was not available, "
                "so only the platform prompts were loaded.", ""]
    else:
        out += [f"This session's name ({name!r}) is not a DevJock task session name, so no task "
                "context was loaded. To tie the session to a task, `/rename` it to "
                "`devjock_{task_id}_{YYYY-MM-DD}_{agent-type}_{task-name}` and run "
                "/devjock:initialize-devjock again.", ""]
    others = _other_sessions_lines(session_id, registry)
    out += _section("Other Claude Code sessions on this machine", lambda: others)
    return "\n".join(out)


def context_path(session_id, data_dir):
    """Per-session file, or None when either value is missing or malformed."""
    if not (_valid_session_id(session_id) and _valid_data_dir(data_dir)):
        return None
    folder = Path(data_dir) / "context"
    folder.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - CONTEXT_MAX_AGE_DAYS * 86400
    for old in folder.glob("*.md"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except Exception:
            pass
    return folder / f"{session_id.strip().lower()}.md"


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


def _parse_args(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--session-id", default="")
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--transcript", default="")
    ap.add_argument("--no-inline", action="store_true",
                    help="never print the full body to stdout (the SessionStart hook's output is capped)")
    try:
        args, _ = ap.parse_known_args(argv)
    except SystemExit:
        # A flag with its value missing (an unquoted, empty substitution) must not stop the
        # prompts loading; it only means no per-session file and no task context.
        args = ap.parse_args([])
    return args


def main(argv=None):
    """Write the context file and print its pointer. Returns a dict for the hook:
    {"path", "working_context", "per_session"}; None when nothing was written."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
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
              f"(agent blueprints). Order mirrors CWO. -->")
    if DEVJOCK_ENV != "production":
        header += f"\n<!-- DEVJOCK_ENV={DEVJOCK_ENV}: loaded from {API} -->"
    if not platform:
        header += f"\n<!-- {NO_PROMPTS_NOTICE} -->"

    platform_md = emit_prompts(platform, "DevJock System Prompts — your operating context (live-injected, preamble)")
    if not platform:
        platform_md += f"\n**{NO_PROMPTS_NOTICE}**\n"

    # Per-session file when both the session id and the plugin data folder came through;
    # otherwise the old shared path, which never carries task context (task 48-41483).
    try:
        out_path = context_path(args.session_id, args.data_dir)
    except Exception:
        out_path = None
    per_session = out_path is not None
    if not per_session:
        out_path = Path(__file__).resolve().parent / "injected-context.md"
    working_md = working_context(args.session_id.strip().lower(), args.transcript or None,
                                 token, templates, include_task=per_session)

    body = "\n".join([
        header,
        "",
        working_md,
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
    try:
        out_path.write_text(body, encoding="utf-8")
    except Exception as e:
        if args.no_inline:
            print(f"*Could not write the DevJock context file ({e}). System prompts NOT injected.*")
            return None
        # Last-resort fallback: emit inline (may be truncated by the harness), but
        # say so loudly so the agent knows to distrust a preview.
        print("*Could not write injected-context file "
              f"({e}); emitting inline — THIS MAY BE TRUNCATED by the harness. "
              "If you see a persisted-output/preview wrapper, the context is INCOMPLETE.*")
        print()
        print(body)
        return None

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
    return {"path": str(out_path), "working_context": working_md, "per_session": per_session}


if __name__ == "__main__":
    main()
