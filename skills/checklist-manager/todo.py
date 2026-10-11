#!/usr/bin/env python3
"""checklist-manager — mechanical checklist manager mirrored into DevJock memories.


PURPOSE: a checklist is a lightweight, ordered, checkable list mirrored into a
DevJock memory. Legitimate uses: loose ends with no task ("call the doctor back in
5 minutes"); a shortlist (100 items are In Progress, these 4 are this morning);
small steps inside one long task, too granular for subtasks; workflow/procedure
tracking; skill-internal checklists (/gmail triage, briefing runs); non-work lists
entirely (a music collection grouped by artist); and triage caching — things that
still need to become real tasks.

THE ONE INVARIANT : a
checklist NEVER replaces the task system. Do not migrate a project's work plan onto
a list and work from it as the backlog. Small items get handled in the course of
work and checked off; bigger items get filed as DevJock tasks, LINKED from the item
(--link + --link-title), and checked off.

CATEGORIES: the default vocabulary is GTD status, and these are the only values
that sort — see CAT_ORDER below (in-progress / in process / in progress,
done-verify, next, waiting / wait, blocked, active, someday, ongoing). Any other
category is allowed for non-GTD lists, but unranked categories sort after all GTD
ones in first-appearance order, which is a fallback and not a design. Within a
category, `rank` orders items explicitly (1 = top); unranked items follow in
creation order. See SKILL.md.

PLATFORM: pure Python 3 + stdlib, no external binaries and no macOS-only APIs, so
it runs on macOS, Linux and Windows. See "Platform support" in SKILL.md for how to
invoke it on Windows (python, not python3; no $P shell variable).

Design contract: item text is IMMUTABLE. Operations are
add / check / note / archive / get / list / push only. There is no edit.
The DevJock memory is a rendered VIEW — never hand-edited, always overwritten
by push.

Data lives OUTSIDE the skill (survives plugin updates): ~/.claude/todo-lists/
Lists are named for their target memory: m3951.jsonl -> memory 3951.
Archived items move to m3951-archive.jsonl and keep their numbers forever.

Usage:
  todo.py list    <list> [--all]                 # numbers + titles (token-cheap)
  todo.py get     <list> <n>                     # one full item (live or archived)
  todo.py add     <list> --title T [--description D] [--link URL] [--link-title "semantic name"] [--category C]
  todo.py check   <list> <n> --note "dated outcome"
  todo.py note    <list> <n> --note "progress update"   # append an update bullet, no state change
  todo.py recat   <list> <n> --category C         # move an item between categories
  todo.py rank    <list> <n> --rank N             # order within a category (1 = top, 0 clears)
  todo.py archive <list>                         # move checked items to the archive file
  todo.py push    <list>                         # render template + upload to the DevJock memory
  todo.py rollup  [--home TASK_ID --user USER_ID] # regenerate the read-only cross-checklist view
Mutating commands auto-push unless --no-push.
"""
import argparse, datetime, glob, io, json, os, re, sys, urllib.request

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.expanduser("~/.claude/todo-lists")
TEMPLATE = os.path.join(SKILL_DIR, "template.html")
API_BASE = "https://api.devjock.com/v1.0"


def _paths(name):
    return (os.path.join(DATA_DIR, f"{name}.jsonl"),
            os.path.join(DATA_DIR, f"{name}-archive.jsonl"))


def _load(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _save(path, items):
    os.makedirs(DATA_DIR, exist_ok=True)
    # newline="\n" so the JSONL is byte-identical on Windows (no CRLF).
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def _today():
    return datetime.date.today().isoformat()


_TOKEN = None


def _jwt():
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    _TOKEN = _find_token()
    return _TOKEN


def _find_token():
    # The plugin's own sign-in (keychain / Windows Credential Manager). A saved token is used
    # as-is: validating it costs an API call per run, and a rate-limited validation would be
    # misread as a bad token and open a browser mid-command. An expired token fails with 401,
    # which tells the user to reauthenticate. The browser opens only when nothing is saved.
    lib = os.path.join(os.path.dirname(SKILL_DIR), "lib")
    if os.path.isdir(lib):
        sys.path.insert(0, lib)
        try:
            import authorize_devjock_api as auth
            saved = auth._load_tokens() or {}
            tok = saved.get("access_token") or auth.get_access_token()
            if tok:
                return tok
        except Exception:
            pass
    # Fallback: a token cached by mcp-remote, where that is installed.
    files = sorted(glob.glob(os.path.expanduser("~/.mcp-auth/*/*_tokens.json")),
                   key=os.path.getmtime, reverse=True)
    for fp in files:
        try:
            with io.open(fp, encoding="utf-8") as fh:
                tok = json.load(fh).get("access_token")
            if tok:
                return tok
        except Exception:
            continue
    sys.exit("ERROR: not signed in to DevJock — run /devjock:reauthenticate, then retry.")


def _memory_id(name):
    m = re.match(r"^m(\d+)$", name)
    if not m:
        sys.exit(f"ERROR: list name '{name}' must look like m<memory_id> (e.g. m3951).")
    return int(m.group(1))


def _entity_id(url):
    """Extract a short DevJock entity id from a URL for the name-first link suffix."""
    for pat, pre in [(r"/tasks/([\d-]+)", ""), (r"/memory/(\d+)", "m"),
                     (r"/prompts/(\d+)", "p"), (r"/chat/(\d+)", "c"),
                     (r"group_id=(\d+)", "e"), (r"sprints=(\d+)", "s")]:
        m = re.search(pat, url)
        if m:
            return pre + m.group(1)
    return ""


def _md_link(it):
    """Semantic, knowledge-graph-friendly link: [meaningful name (id)](url)."""
    url = it.get("link", "")
    if not url:
        return ""
    name = it.get("link_title") or it["title"]
    eid = _entity_id(url)
    label = f"{name} ({eid})" if eid and f"({eid})" not in name else name
    return f"[{label}]({url})"


def _fmt_item(it, full=False):
    box = "x" if it.get("checked") else " "
    head = f"{it['n']}. [{box}] {it['title']}"
    if not full:
        return head
    lines = [head]
    if it.get("category"):
        lines.append(f"   category: {it['category']}")
    if it.get("link"):
        lines.append(f"   link: {it['link']}")
    if it.get("description"):
        lines.append(f"   {it['description']}")
    for u in it.get("updates", []):
        lines.append(f"   update {u['date']}: {u['note']}")
    if it.get("checked"):
        lines.append(f"   DONE {it['checked']['date']}: {it['checked']['note']}")
    lines.append(f"   added: {it.get('created', '?')}")
    return "\n".join(lines)


def _render_item(it):
    box = "x" if it.get("checked") else " "
    lines = [f"- [{box}] **{it['n']}. {it['title']}**"]
    link = _md_link(it)
    if link:
        lines.append(f"  - {link}")
    if it.get("description"):
        lines.append(f"  - {it['description']}")
    for u in it.get("updates", []):
        lines.append(f"  - *update {u['date']}:* {u['note']}")
    if it.get("checked"):
        lines.append(f"  - ✅ *done {it['checked']['date']}:* {it['checked']['note']}")
    return "\n".join(lines)


# Categories are GTD statuses using the platform's OWN names and order, read from the
# status table via ensure_context (p447 §4), so a checklist reads like a task board.
# In Process = right now; Next = after the In Process pile; Active = on the short list
# but not today; Someday = no near-term plan. Wait/Blocked cross-cut. Unknown sorts last.
CAT_ORDER = ["in-progress", "in process", "in progress", "done-verify",
             "next", "waiting", "wait", "blocked", "active",
             "someday", "ongoing"]


def _item_sort_key(it):
    """Within a category: explicit rank first (1 = top), then item number.
    An unranked item falls below every ranked one, in creation order."""
    r = it.get("rank")
    try:
        r = int(r)
    except (TypeError, ValueError):
        r = None
    return (0, r, it["n"]) if r is not None else (1, 0, it["n"])


def _cat_rank(c):
    c = (c or "").strip().lower()
    return CAT_ORDER.index(c) if c in CAT_ORDER else len(CAT_ORDER)


def _grouped(items):
    cats = {}
    for it in items:
        cats.setdefault(it.get("category") or "uncategorized", []).append(it)
    out = []
    cats = {c: cats[c] for c in sorted(cats, key=_cat_rank)}  # GTD order
    for c in cats:  # dict preserves insertion order = first appearance in the file
        out.append(f"## {c.upper()}")
        out.extend(_render_item(it) for it in sorted(cats[c], key=_item_sort_key))
    return "\n\n".join(out)


def _memory_title(name):
    try:
        req = urllib.request.Request(
            f"{API_BASE}/memories/get?memory_id={_memory_id(name)}",
            headers={"Authorization": f"Bearer {_jwt()}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        for k in ("memory", "data"):
            if isinstance(data.get(k), dict):
                data = data[k]; break
        return data.get("name") or name
    except Exception:
        return name


_DISCOVER_CACHE = None


def discover_cached():
    """discover() is several API calls; a single render needs it twice. Cache per run."""
    global _DISCOVER_CACHE
    if _DISCOVER_CACHE is None:
        _DISCOVER_CACHE = discover()
    return _DISCOVER_CACHE


def _crumbs(r):
    """Workspace › Epic › Parent. Grey, quiet, but clickable — context, not a call to action."""
    parts = []
    if r.get("workspace"):
        w = str(r["workspace"]).replace("<", "&lt;")
        parts.append(f'<a target="_blank" rel="noopener" href="https://www.devjock.com/epics/?workspace={r["workspace_id"]}">'
                     f'{w} (w{r["workspace_id"]})<span class="ext">\u2197</span></a>' if r.get("workspace_id") else w)
    if r.get("epic"):
        ep = r["epic"].replace("<", "&lt;")
        parts.append(f'<a target="_blank" rel="noopener" href="https://www.devjock.ai/projects?group_id={r["epic_id"]}">'
                     f'{ep} (e{r["epic_id"]})<span class="ext">\u2197</span></a>' if r.get("epic_id") else ep)
    if r.get("parent"):
        pt = r["parent"].replace("<", "&lt;")
        parts.append(f'<a target="_blank" rel="noopener" href="https://www.devjock.ai/tasks/{r["parent_id"]}">'
                     f'{pt} ({r["parent_id"]})<span class="ext">\u2197</span></a>' if r.get("parent_id") else pt)
    return " &rsaquo; ".join(parts)


def _nav_html(current):
    """Collapsible cross-links to every other checklist, discovered live by label.

    Name-first per the canonical linking standard (p323). Breadcrumb goes ABOVE the
    name so the context reads before the thing it describes.

    LINKS: no target= value can fix left-click here, so do not go hunting for one.
    Verified in real Chrome against sandbox="allow-scripts" (2026-08-19):
      window.open / target="_blank" -> "Blocked opening ... sandboxed frame whose
                                        'allow-popups' permission is not set", returns null
      target="_top" / "_parent"     -> SecurityError, "'allow-top-navigation' ... is not set"
      no target                     -> navigates the frame itself into a DevJock error
    Left-click is instead intercepted in template.html, which runs a fallback chain
    ending in copy-the-URL-to-the-clipboard with a visible "copied" badge (the async
    Clipboard API is also blocked by permissions policy in this frame, so it uses
    execCommand). The href stays intact so right-click -> open in new tab still works.
    target="_blank" rel="noopener" is kept only so the links behave correctly if this
    HTML is ever viewed outside the sandbox.
    """
    try:
        rows = discover_cached()
    except Exception:
        return ""
    if len(rows) < 2:
        return ""
    lis = []
    for r in sorted(rows, key=lambda x: (x["title"] or "").lower()):
        title = (r["title"] or "").replace("<", "&lt;")
        counts = _local_counts(r["list"]) if r["list"] else None
        meta = f' <span class="meta">— {counts[0]} open</span>' if counts else ""
        if r["list"] == current:
            name = f'<span class="here">{title} (m{r["memory_id"]})</span>{meta} <span class="meta">— you are here</span>'
        else:
            name = (f'<a target="_blank" rel="noopener" href="https://www.devjock.ai/memory/{r["memory_id"]}">'
                    f'{title} (m{r["memory_id"]})<span class="ext">\u2197</span></a>{meta}')
        crumb = _crumbs(r)
        lis.append("<li>"
                   + (f'<div class="crumb">{crumb}</div>' if crumb else "")
                   + f'<div class="name">{name}</div></li>')
    return ('<details class="nav"><summary>View all checklists</summary><ul>'
            + "".join(lis) + "</ul></details>")


def _list_title(name):
    """Prefer the owning checklist TASK's title. The memory is a generated view named
    CHECKLIST; the task is what carries the human-meaningful name, so names cannot
    drift apart."""
    try:
        for r in discover_cached():
            if r["list"] == name:
                return r["title"]
    except Exception:
        pass
    return _memory_title(name)


def render(name):
    live_path, arch_path = _paths(name)
    items, archived = _load(live_path), _load(arch_path)
    for it in items + archived:
        it["eid"] = _entity_id(it.get("link", ""))
    with io.open(TEMPLATE, encoding="utf-8") as fh:
        tpl = fh.read()
    stamp = (
        f"⚙ This checklist is generated by the <b>checklist-manager</b> skill in the "
        f"<b>devjock</b> Claude Code plugin (list id: {name}, rendered {_today()}). "
        f"To update it, tell Claude Code e.g. &quot;check off item 4 on {name}&quot; or "
        f"&quot;add to {name}: …&quot; — do NOT edit this memory by hand; it is regenerated "
        f"in full on every change, and its name is kept identical to its task's title.<br>"
        f"<b>How a checklist is made:</b> create a task, give it the <b>checklist</b> label, "
        f"and set this memory as the task's default memory. The label is the index — the "
        f"skill finds every checklist by querying that label, which is why the list above "
        f"stays complete without anyone maintaining it.")
    data = json.dumps({"items": items, "archive": archived}, ensure_ascii=False)
    title = _list_title(name)
    tpl = tpl.replace("{{NAV}}", _nav_html(name))
    return (tpl.replace("{{TITLE}}", title.replace("<", "&lt;"))
               .replace("{{DATA}}", data).replace("{{STAMP}}", stamp))


def push(name):
    mem_id = _memory_id(name)
    content = render(name)
    payload = {"content": content, "file_type_id": 3}
    title = _list_title(name)
    if title and title != name:
        payload["name"] = title   # keep memory name == task title == H1
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/memories/update?memory_id={mem_id}", data=body, method="POST",
        headers={"Authorization": f"Bearer {_jwt()}",
                 "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    print(f"pushed -> memory {mem_id}: {result}")
    if _rollup_cfg():
        try:
            rollup(quiet=True)
        except Exception as e:
            print(f"  (rollup not refreshed: {e})")


# Every checklist is a task carrying this label, and its DEFAULT MEMORY is the list.
# The label IS the index — there is deliberately no registry memory listing the lists,
# because a hand-maintained index goes stale on the first change (p351 §4) and because
# a local file scan can only ever see lists made on this machine.
CHECKLIST_LABEL_ID = 17


def _api(path, body=None):
    """GET by default; pass a dict body to POST (tasks/single is POST-only)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {_jwt()}", "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API_BASE}/{path}", data=data, headers=headers,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def discover():
    """Find every checklist task the user can see, across all workspaces."""
    data = _api(f"tasks/list?label_id={CHECKLIST_LABEL_ID}"
                "&lifecycle_category=all&page_size=200&resolves=workspace")
    # The list endpoint returns 'tasks' and does NOT carry default_memory_id, so the
    # default memory (which IS the checklist) has to come from a per-task read.
    rows = []
    for t in data.get("tasks", data.get("items", [])):
        tid = t.get("task_id")
        mem = grp = par = None
        try:
            single = _api("tasks/single", {"task_id": tid,
                                          "resolves": ["default_memory_id",
                                                       "groups", "parent_task"]})
            for k in ("task", "data"):
                if isinstance(single.get(k), dict):
                    single = single[k]; break
            mem = single.get("default_memory_id")
            grp = (single.get("groups") or [None])[0]
            par = single.get("parent_task")
        except Exception:
            grp = par = None
        ws = t.get("workspace") or {}
        rows.append({"task_id": tid, "title": t.get("title", ""),
                     "workspace": ws.get("name") or t.get("workspace_id"),
                     "workspace_id": t.get("workspace_id"),
                     "epic": (grp or {}).get("name") if isinstance(grp, dict) else None,
                     "epic_id": (grp or {}).get("id") if isinstance(grp, dict) else None,
                     "parent": (par or {}).get("title") if isinstance(par, dict) else None,
                     "parent_id": (par or {}).get("task_id") if isinstance(par, dict) else None,
                     "list": f"m{mem}" if mem else None, "memory_id": mem})
    # Lists on this machine whose memory is not reached through a labelled task
    # (TODO lists born on MEETING tasks, whose one label and default memory belong
    # to the meeting) still count as checklists: look each
    # one up by its memory so `lists` and the rollup see every checklist.
    seen = {r["list"] for r in rows}
    for fp in sorted(glob.glob(os.path.join(DATA_DIR, "m*.jsonl"))):
        name = os.path.basename(fp)[:-6]
        if name in seen or name.endswith("-archive"):
            continue
        try:
            m = _api(f"memories/get?memory_id={_memory_id(name)}")
            for k in ("memory", "data"):
                if isinstance(m.get(k), dict):
                    m = m[k]; break
            tc = m.get("task_context") or {}
            rows.append({"task_id": tc.get("task_id"), "title": m.get("name") or name,
                         "workspace": f"workspace {m.get('workspace_id')}",
                         "workspace_id": m.get("workspace_id"),
                         "epic": None, "epic_id": None,
                         "parent": tc.get("title"), "parent_id": tc.get("task_id"),
                         "list": name, "memory_id": _memory_id(name), "by_label": False})
        except Exception:
            continue
    return rows


def _local_counts(list_name):
    """Open/total for a list whose JSONL is on this machine; None if it is not."""
    live, arch = _paths(list_name)
    if not os.path.exists(live):
        return None
    items = _load(live)
    return sum(1 for i in items if not i.get("checked")), len(items)



# ------------------------------------------------------------------ rollup
# ONE generated view across every checklist plus the user's live In Process
# tasks, so an EA (human or agent) works from a single page. It is a VIEW, never a
# list: no item lives here, so nothing is entered twice. Regenerated on every push
# of any list (see push), and by whoever else calls `todo.py rollup` (EA boot, EA run).
ROLLUP_CFG = os.path.join(DATA_DIR, "rollup.json")
ROLLUP_NAME = "ROLLUP: work across all checklists"
ROLLUP_CATS = ("in process", "in progress", "next")


def _rollup_cfg():
    if os.path.exists(ROLLUP_CFG):
        with io.open(ROLLUP_CFG, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def _rollup_items():
    """[(category, item, list_row)] for open items in ROLLUP_CATS across all
    checklists whose data is on this machine, ordered In Process first, then Next."""
    out = []
    for r in discover_cached():
        if not r["list"]:
            continue
        live, _ = _paths(r["list"])
        if not os.path.exists(live):
            continue
        for it in _load(live):
            if it.get("checked"):
                continue
            cat = (it.get("category") or "").strip().lower()
            if cat in ROLLUP_CATS:
                out.append(("in process" if cat.startswith("in p") else "next", it, r))
    out.sort(key=lambda x: (0 if x[0] == "in process" else 1))
    return out


def _rollup_tasks(user_id):
    """Live In Process tasks assigned to user_id, grouped workspace > parent."""
    data = _api(f"tasks/list?page=1&page_size=100&status_id=16&assigned_to={user_id}"
                "&lifecycle_category=all&resolves=parent,workspace,groups,dates")
    tasks = [t for t in data.get("tasks", []) if not (t.get("closed_date") or "")[:10]]
    tree = {}
    for t in tasks:
        ws = t.get("workspace") or {}
        wkey = (t.get("workspace_id"), ws.get("name") or f"workspace {t.get('workspace_id')}")
        par = t.get("parent_task") or t.get("parent")
        pkey = (par.get("task_id"), par.get("title")) if isinstance(par, dict) and par.get("title") else (None, "(no parent)")
        tree.setdefault(wkey, {}).setdefault(pkey, []).append(t)
    return tree, len(tasks)


def _task_link(tid, title):
    return f"[{title} ({tid})](https://www.devjock.ai/tasks/{tid})"


def _render_section(label, mid, lines):
    """One extra live section: a slice of an existing memory, pulled by id."""
    try:
        m = _api(f"memories/get?memory_id={mid}")
        body = (m.get("content") or "").strip().splitlines()
        out = [f"## {label}", "",
               f"_Source: [{m.get('name')} (m{mid})](https://www.devjock.ai/memory/{mid})_", ""]
        out += body[:lines]
        if len(body) > lines:
            out.append(f"_… {len(body) - lines} more lines in the source_")
        out.append("")
        return out
    except Exception as e:
        return [f"## {label}", "", f"* could not read memory {mid}: {e}", ""]


def _split_sections(cfg):
    """Sections are [label, memory_id, lines] or [label, memory_id, lines, "top"].

    A section flagged "top" renders directly under the header, ABOVE the
    checklists and the task tree, because the first screen of this memory is
    what the reader actually reads. Anything else renders at the bottom as before,
    so existing configs keep their old behaviour.
    """
    top, bottom = [], []
    for s in cfg.get("sections", []):
        label, mid, lines = s[0], s[1], s[2]
        where = (s[3] if len(s) > 3 else "bottom") or "bottom"
        (top if str(where).lower() == "top" else bottom).append((label, mid, lines))
    return top, bottom


def render_rollup(cfg):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    L = [f"# {ROLLUP_NAME}", "",
         f"_Generated {now} by `todo.py rollup`. A read-only VIEW: nothing lives here. "
         f"Rebuilt on every checklist push, every EA run and every EA boot; if this stamp is old, "
         f"run `todo.py rollup`. Home: [{cfg.get('home_title') or cfg['task_id']} ({cfg['task_id']})]"
         f"(https://www.devjock.ai/tasks/{cfg['task_id']})._", ""]
    top_sections, bottom_sections = _split_sections(cfg)
    # --- pinned sections (the first screen)
    for label, mid, lines in top_sections:
        L += _render_section(label, mid, lines)
    # --- checklists
    items = _rollup_items()
    L += ["## Across all checklists", ""]
    ea = cfg.get("ea_list")
    if ea:
        ea_open = [i for i in _load(_paths(ea)[0]) if not i.get("checked")]
        ea_open.sort(key=lambda i: (_cat_rank(i.get("category")), _item_sort_key(i)))
        L += [f"### Next five for the EA", "",
              f"_The EA's own checklist: [{_list_title(ea)} ({ea})](https://www.devjock.ai/memory/{_memory_id(ea)}) "
              f"— {len(ea_open)} open._", ""]
        for it in ea_open[:5]:
            link = f" · [{it['link_title']}]({it['link']})" if it.get("link") and it.get("link_title") else ""
            L.append(f"* {it['title']} · #{it['n']}{link}")
        L.append("")
    rows_all = [r for r in discover_cached() if r["list"]]
    L += [f"### Checklists in this rollup ({len(rows_all)})", ""]
    for r in sorted(rows_all, key=lambda x: (x["title"] or "").lower()):
        c = _local_counts(r["list"])
        cnt = f"{c[0]} open / {c[1]}" if c else "data not on this machine"
        L.append(f"* [{r['title']} (m{r['memory_id']})](https://www.devjock.ai/memory/{r['memory_id']}) · {cnt}")
    L.append("")
    if not items:
        L.append("* nothing In Process or Next on any checklist")
    for section in ("in process", "next"):
        rows = [x for x in items if x[0] == section]
        if not rows:
            continue
        L += [f"### {section.title()}", ""]
        for _, it, r in rows:
            home = f"[{r['title']} (m{r['memory_id']})](https://www.devjock.ai/memory/{r['memory_id']})"
            link = f" · [{it['link_title']}]({it['link']})" if it.get("link") and it.get("link_title") else ""
            L.append(f"* {it['title']} · #{it['n']} on {home}{link}")
        L.append("")
    # --- real to-do
    tree, n = _rollup_tasks(cfg["user_id"])
    L += [f"## In Process tasks assigned to me ({n})", ""]
    for (wid, wname), parents in sorted(tree.items(), key=lambda kv: str(kv[0][1])):
        L.append(f"* **{wname}** (w{wid})")
        for (pid, ptitle), kids in sorted(parents.items(), key=lambda kv: kv[0][1]):
            L.append(f"  * {_task_link(pid, ptitle) if pid else ptitle}")
            for t in kids:
                due = f" · due {t['due_date'][:10]}" if t.get("due_date") else ""
                L.append(f"    * {_task_link(t['task_id'], t['title'])}{due}")
    L.append("")
    # --- extra live sections, each an existing memory pulled by id
    for label, mid, lines in bottom_sections:
        L += _render_section(label, mid, lines)
    for label, url in cfg.get("links", []):
        L.append(f"* {label}: {url}")
    return "\n".join(L)


def _md_html(md):
    """Small markdown -> HTML for the rollup's own output: headings, nested bullets,
    links, bold, italics. Enough for what render_rollup and the pulled sections emit."""
    def inline(t):
        t = t.replace("&", "&amp;").replace("<", "&lt;")
        t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                   r'<a href="\2" target="_blank" rel="noopener">\1</a>', t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<![\w*])_(.+?)_(?!\w)", r"<i>\1</i>", t)
        t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
        return t
    out, depth = [], 0
    def close(to):
        nonlocal depth
        while depth > to:
            out.append("</li></ul>"); depth -= 1
    for line in md.splitlines():
        m = re.match(r"^(\s*)[*-] (.*)$", line)
        if m:
            d = len(m.group(1)) // 2 + 1
            if d > depth:
                while depth < d:
                    out.append("<ul><li>" if depth + 1 == d else "<ul><li>"); depth += 1
                out[-1] += inline(m.group(2))
            else:
                close(d); out.append("</li><li>" + inline(m.group(2)))
            continue
        close(0)
        h = re.match(r"^(#{1,4}) (.*)$", line)
        if h:
            lvl = len(h.group(1))
            if lvl == 1:
                continue  # the H1 is the template's title
            out.append(f'<h2 class="cat">{inline(h.group(2))}</h2>' if lvl == 2
                       else f"<h{lvl}>{inline(h.group(2))}</h{lvl}>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    close(0)
    return "\n".join(out)


def _rollup_html(md):
    """Wrap the rollup in the checklist template so it looks and links like the lists."""
    with io.open(TEMPLATE, encoding="utf-8") as fh:
        tpl = fh.read()
    stamp = (f"⚙ This rollup is generated by <code>todo.py rollup</code> in the <b>checklist-manager</b> "
             f"skill (rendered {_today()}). A read-only VIEW across every checklist: add, note or check "
             f"items on their home checklist, never here.")
    body = '<div id="djt-rollup">' + _md_html(md) + '</div>'
    tpl = (tpl.replace("{{TITLE}}", ROLLUP_NAME).replace("{{NAV}}", _nav_html(None))
              .replace("{{DATA}}", '{"items": [], "archive": []}').replace("{{STAMP}}", stamp))
    tpl = re.sub(r'<p class="purpose">.*?</p>', "", tpl, count=1, flags=re.S)
    tpl = tpl.replace('<div class="bar">', '<div class="bar hidden">')
    return tpl.replace('<div id="djt-list"></div>', body + '<div id="djt-list" class="hidden"></div>')


def rollup(home=None, user_id=None, quiet=False):
    cfg = _rollup_cfg() or {}
    if home:
        cfg["task_id"] = home
    if user_id:
        cfg["user_id"] = int(user_id)
    if not cfg.get("task_id") or not cfg.get("user_id"):
        sys.exit("rollup needs --home TASK_ID and --user USER_ID the first time (saved to "
                 f"{ROLLUP_CFG})")
    if not cfg.get("memory_id"):
        # find or create the rollup memory on the home task
        found = None
        try:
            lst = _api(f"memories/list?task_id={cfg['task_id']}&page_size=50")
            for m in lst.get("data") or lst.get("memories") or []:
                if (m.get("name") or "").startswith("ROLLUP:") and m.get("active", True):
                    found = m.get("id")
        except Exception:
            pass
        if not found:
            created = _api("memories/create", {"task_id": cfg["task_id"], "name": ROLLUP_NAME,
                                               "content": "(building)", "file_type_id": 4})
            found = created.get("memory_id") or created.get("id")
        cfg["memory_id"] = found
        try:
            t = _api("tasks/single", {"task_id": cfg["task_id"], "resolves": ["title"]})
            for k in ("task", "data"):
                if isinstance(t.get(k), dict):
                    t = t[k]; break
            cfg["home_title"] = t.get("title")
        except Exception:
            pass
    os.makedirs(DATA_DIR, exist_ok=True)
    with io.open(ROLLUP_CFG, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cfg, fh, indent=1)
    content = render_rollup(cfg)
    _api(f"memories/update?memory_id={cfg['memory_id']}",
         {"content": _rollup_html(content), "file_type_id": 3, "name": ROLLUP_NAME})
    if not quiet:
        print(f"rollup -> memory {cfg['memory_id']} on {cfg['task_id']} "
              f"({content.count(chr(10))} lines) https://www.devjock.ai/memory/{cfg['memory_id']}")
    return cfg["memory_id"]

def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["lists", "list", "get", "add", "check", "note", "recat", "rank", "archive", "push", "rollup"])
    p.add_argument("--home", help="rollup: home task id, e.g. 51-150 (first run)")
    p.add_argument("--user", help="rollup: user id whose In Process tasks to show (first run)")
    p.add_argument("name", nargs="?", help="list name, e.g. m3951 (not needed for 'lists')")
    p.add_argument("n", nargs="?", type=int, help="item number (get/check/note)")
    p.add_argument("--title"); p.add_argument("--description", default="")
    p.add_argument("--link", default=""); p.add_argument("--link-title", default="")
    p.add_argument("--category", default=""); p.add_argument("--note")
    p.add_argument("--rank", type=int, default=None)
    p.add_argument("--all", action="store_true"); p.add_argument("--no-push", action="store_true")
    a = p.parse_args()

    if a.command == "rollup":
        rollup(home=a.home, user_id=a.user); return

    if a.command == "lists":
        rows = discover()
        if not rows:
            print("no tasks carry the checklist label yet"); return
        print("YOUR CHECKLISTS  (discovered by label, all workspaces)\n")
        for r in rows:
            c = _local_counts(r["list"]) if r["list"] else None
            state = f"{c[0]} open / {c[1]} items" if c else ("data not on this machine" if r["list"] else "NO DEFAULT MEMORY SET")
            print(f"  {r['list'] or '--':>8}  {r['title']}")
            print(f"            {state}  ·  ws {r['workspace']}  ·  https://www.devjock.ai/tasks/{r['task_id']}")
        print(f"\n-- {len(rows)} checklist(s)")
        return

    if not a.name:
        sys.exit(f"'{a.command}' needs a list name, e.g. m3951")
    live_path, arch_path = _paths(a.name)
    items = _load(live_path)

    if a.command == "list":
        print("[checklist — real work belongs on DevJock tasks; link items that become tasks]")
        shown = items if a.all else [i for i in items if not i.get("checked")]
        # Group by GTD status in the same order the rendered memory uses, so the
        # CLI and the published dashboard never disagree about what is on top.
        buckets = {}
        for i in shown:
            buckets.setdefault(i.get("category") or "uncategorized", []).append(i)
        out = []
        for c in sorted(buckets, key=_cat_rank):
            out.append(f"\n[{c.upper()}]")
            out.extend(_fmt_item(i) for i in sorted(buckets[c], key=_item_sort_key))
        print("\n".join(out).strip() or "(empty)")
        done = sum(1 for i in items if i.get("checked"))
        print(f"-- {len(items)} live ({done} checked, unarchived); {len(_load(arch_path))} archived")
        return

    if a.command == "get":
        if a.n is None: sys.exit("get needs an item number")
        pool = {i["n"]: i for i in items + _load(arch_path)}
        if a.n not in pool: sys.exit(f"no item {a.n}")
        print(_fmt_item(pool[a.n], full=True)); return

    if a.command == "add":
        if not a.title: sys.exit("add needs --title")
        nums = [i["n"] for i in items + _load(arch_path)]
        it = {"n": max(nums, default=0) + 1, "title": a.title,
              "description": a.description, "link": a.link,
              "link_title": a.link_title, "category": a.category,
              "created": _today(), "updates": [], "checked": None}
        items.append(it); _save(live_path, items)
        print(_fmt_item(it, full=True))

    elif a.command == "check":
        if a.n is None or not a.note: sys.exit("check needs an item number and --note")
        hit = next((i for i in items if i["n"] == a.n), None)
        if not hit: sys.exit(f"no live item {a.n}")
        if hit.get("checked"): sys.exit(f"item {a.n} already checked on {hit['checked']['date']}")
        hit["checked"] = {"date": _today(), "note": a.note}
        _save(live_path, items); print(_fmt_item(hit, full=True))

    elif a.command == "note":
        if a.n is None or not a.note: sys.exit("note needs an item number and --note")
        hit = next((i for i in items if i["n"] == a.n), None)
        if not hit: sys.exit(f"no live item {a.n}")
        hit.setdefault("updates", []).append({"date": _today(), "note": a.note})
        _save(live_path, items); print(_fmt_item(hit, full=True))

    elif a.command == "recat":
        # Category is GTD status metadata, NOT item text — moving an item between
        # statuses is a normal edit and does not violate the immutable-text rule.
        if a.n is None or not a.category: sys.exit("recat needs an item number and --category")
        hit = next((i for i in items if i["n"] == a.n), None)
        if not hit: sys.exit(f"no live item {a.n}")
        old = hit.get("category") or "uncategorized"
        hit["category"] = a.category
        _save(live_path, items); print(f"item {a.n}: {old} -> {a.category}")

    elif a.command == "rank":
        # Rank is ordering metadata, not item text. 1 = top of its category.
        # Pass --rank 0 to clear it and drop the item back to creation order.
        if a.n is None or a.rank is None:
            sys.exit("rank needs an item number and --rank N (1 = top, 0 clears)")
        hit = next((i for i in items if i["n"] == a.n), None)
        if not hit:
            sys.exit(f"no live item {a.n}")
        old = hit.get("rank")
        if a.rank == 0:
            hit.pop("rank", None)
            print(f"item {a.n}: rank {old} -> cleared")
        else:
            hit["rank"] = a.rank
            print(f"item {a.n}: rank {old} -> {a.rank}")
        _save(live_path, items)

    elif a.command == "archive":
        keep = [i for i in items if not i.get("checked")]
        move = [i for i in items if i.get("checked")]
        if not move: print("nothing checked to archive"); return
        _save(arch_path, _load(arch_path) + move); _save(live_path, keep)
        print(f"archived {len(move)} item(s): {', '.join(str(i['n']) for i in move)}")

    elif a.command == "push":
        push(a.name); return

    if not a.no_push:
        push(a.name)


if __name__ == "__main__":
    import urllib.error
    try:
        main()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("ERROR: DevJock sign-in expired (HTTP 401) — run /devjock:reauthenticate, then `push` again.")
        raise
