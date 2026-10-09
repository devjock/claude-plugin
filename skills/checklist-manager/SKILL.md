---
name: checklist-manager
description: Create and maintain checklists that live as DevJock memories — ordered, numbered, checkable lists mutated only by Python so item text can never be corrupted by an LLM rewrite. Use for ANY checklist: loose ends with no task ("call the doctor back in 5 minutes"), a shortlist of what you're doing this morning pulled from a big In Progress pile, small steps inside one long task, workflow/procedure tracking, skill-internal checklists, or non-work lists entirely. READ THIS SKILL BEFORE CREATING OR EDITING ANY CHECKLIST — the valid category values and the wiring steps are here and are not guessable. THE ONE INVARIANT: a checklist never replaces the task system; real work becomes a task and the item links to it. Triggers on "checklist", "check off item N", "add to the list", "what's item 8", "make me a list", or a reference to a list id like m3951.
---

# checklist-manager — checklists as DevJock memories

The failure this replaces: keeping a list as one big DevJock memory forces the LLM to
rewrite the whole document for every checkbox, and LLM rewrites corrupt lists (items
paraphrased, renumbered, deleted). Here the list lives in a local JSONL file, mutated only
by Python with append/check semantics — existing item text physically cannot change — and
every change re-renders the DevJock memory from `template.html`, an interactive artifact
(filter bar to show/hide completed items and the archive) pushed as a code-type memory via
the API. No memory content ever passes through the model, which also saves tokens.

## What a checklist is for

A checklist is a lightweight, ordered, checkable list mirrored into a DevJock memory.
Legitimate uses include:

- **Loose ends with no task** — "call the doctor back in 5 minutes"
- **A shortlist** — 100 things are In Progress; these 4 are this morning
- **Small steps inside one long task** — too granular to be worth subtasks
- **Workflow / procedure tracking** — the steps of a repeatable process
- **Skill-internal checklists** — the steps a skill tracks while it runs
- **Non-work lists** — a music collection grouped by artist is a perfectly good checklist
- **Triage cache** — things that still need to become real DevJock tasks

### The one invariant

**A checklist NEVER replaces the task system.** Do not migrate a project's work plan onto
a list and then work from the list as if it were the backlog. If an item is real work, it
becomes a DevJock task and the checklist item **links to that task** (`--link` +
`--link-title`). Small items get handled in the course of work and checked off; bigger
items get filed as tasks, linked, and checked off.

*(Historical note: an earlier version of this file declared every list a "triage cache."
That is one use case, not the purpose. The guardrail above is the part that matters.)*

## Wiring a new checklist

Do these three steps or the list is invisible to the skill's own discovery:

1. **Create (or pick) the DevJock task** the list belongs to.
2. **Give that task the `checklist` label.** The label is the index — the skill finds every
   checklist by querying it, which is how the list of lists stays complete with nobody
   maintaining it.
3. **Set the memory as that task's `default_memory_id`.**

Then create the local list: the list name encodes the memory, so `m4693` → memory 4693.

## Commands

```bash
P="$(dirname "$0")/todo.py"   # or the absolute path to this skill's todo.py
python3 $P list m3951                # open items, numbers + titles only (cheap)
python3 $P list m3951 --all          # include checked-but-unarchived
python3 $P get m3951 8               # one full item (live or archived) — token-cheap single read
python3 $P add m3951 --title "..." --description "..." \
    --link "https://www.devjock.ai/tasks/12-345" \
    --link-title "Book the venue for the offsite" --category next
python3 $P note m3951 8 --note "progress: regroup scheduled"    # append an update bullet
python3 $P check m3951 8 --note "done: shipped as blog 343115"  # dated outcome, immutable after
python3 $P recat m3951 8 --category blocked                     # move an item between categories
python3 $P rank m3951 8 --rank 1                                # order within a category (1 = top, 0 clears)
python3 $P archive m3951             # move checked items to the archive file (numbers preserved)
python3 $P push m3951                # re-render + upload only (mutating commands auto-push)
python3 $P rollup --home 51-150 --user 2359   # first run: ONE read-only view across ALL checklists
python3 $P rollup                    # regenerate it (also runs after every push, once configured)
```

Add `--no-push` to any mutating command to batch changes, then `push` once. Do this when
seeding a list — every add otherwise pushes, and a long seed will hit an HTTP 429.

## Rollup

`rollup` renders one memory — `ROLLUP: work across all checklists` — on the home task, as the
same interactive HTML artifact the lists use. It contains, in order: any sections flagged
`top` in the config; the EA's own next five; an index of every checklist in the rollup with
open/total counts; In Process then Next items from every checklist on this machine (each
naming its home list); the user's live In Process tasks as a tree (workspace > parent >
task); then any remaining configured sections.

Config lives in `~/.claude/todo-lists/rollup.json`: `sections: [[label, memory_id, lines]]`
or `[[label, memory_id, lines, "top"]]` to pin a section above the fold, `links: [[label,
url]]`, and `ea_list` for the EA's own checklist.

Discovery finds checklists two ways: by the `checklist` label (the index), and — since
2026-09-18 — by looking up any `m*.jsonl` on this machine whose memory is not reached
through a labelled task, so lists born on meeting tasks still appear.

It is a VIEW, never a list: add, note or check items on their home checklist, never here.
The stamp in its header is the freshness signal.

## Categories and ordering

`--category` controls grouping AND the order groups render in.

**The default vocabulary is GTD status, and these are the only values that sort:**

```
in-progress / in process / in progress → done-verify → next → waiting / wait
  → blocked → active → someday → ongoing
```

Use these unless there's a reason not to. They give a true priority order: what you're on
now, what's next, what's live but not today, what's waiting, what's stuck, what's later,
what's continuous.

**Any other category is allowed** — "Beatles", "Miles Davis", "Phase 1" are all fine for a
list that isn't GTD-shaped. But be aware: **unranked categories sort after all GTD ones, in
first-appearance order** (the lowest item number in each category decides). That is a
fallback, not a design — if you need a deliberate order for non-GTD categories, set it
explicitly rather than relying on insertion order.

Getting this wrong is easy and looks like a bug: seed "Shopping" as item 1 and it renders
above everything regardless of importance.

**Ordering inside a category** is `rank` (`--rank 1` = top of its category, `--rank 0`
clears it). Rank is ordering metadata, not item text, so setting it does not violate the
immutability contract. Unranked items fall below every ranked one, in creation order.

## Data model

- Data lives OUTSIDE the plugin so updates never wipe it: `~/.claude/todo-lists/<list>.jsonl` (+ `<list>-archive.jsonl`).
- The list name encodes the target memory: `m3951` → memory 3951. A new list = a new `m<id>` name pointing at an existing memory.
- Item columns: number (permanent, never reused), title, description, link + link_title, category, created date, updates[], checked{date, note}.
- Rendered view: interactive HTML artifact (code-type memory) — grouped by category, filter bar up top to show/hide completed items and the archive, live counts.
- **Semantic links for the knowledge graph**: `link_title` must be the entity's real name (e.g. the task title), never a bare URL or ID — the words inside the link carry the semantic meaning. The renderer appends the short entity id automatically per the linking standard (p323).

## Rules for the agent

1. NEVER update a managed memory with update_memory — `push` owns it. The memory carries a "managed mechanically" stamp.
2. NEVER edit `~/.claude/todo-lists/*.jsonl` by hand — commands only. There is deliberately no edit command; a wrong item gets checked with a note ("superseded by item N") and replaced by a new item.
3. The list belongs to the human, and only the human closes an item: run `check` when they say a named item is done, not when the work looks finished to you. Until then, progress goes in via `note`, and the check-off note is the human's dated outcome in plain English (date stamped automatically).
4. Read with `get`/`list`, never by pulling the memory — that is the whole point.
5. When an item becomes a real task, link it (`--link` + `--link-title`) rather than leaving the list as a parallel copy of the work.

## Platform support

`todo.py` is pure Python 3 standard library — no external binaries, no `subprocess`, no
hardcoded absolute paths. It runs on **macOS and Windows**, where the plugin keeps your
DevJock sign-in in the system credential store. On Linux there is no such store, so it
works only where mcp-remote has cached a DevJock token.

- Paths use `os.path.expanduser` / `os.path.join`, so `~/.claude/todo-lists` resolves to
  `%USERPROFILE%\.claude\todo-lists` on Windows.
- Data files are written UTF-8 with `newline="\n"`, so the JSONL is byte-identical across
  platforms and a list synced between machines will not churn.
- **Invocation on Windows:** the `#!/usr/bin/env python3` shebang is ignored and the
  `P="$(dirname "$0")/todo.py"` idiom above is POSIX shell. Use PowerShell and the full
  path with the `python` launcher, e.g.
  `python "$env:USERPROFILE\.claude\plugins\...\checklist-manager\todo.py" list m3951`.
  Everything after the script path is identical on every platform.
- Auth uses the plugin's DevJock sign-in, the same one `initialize-devjock` uses.

## Auth

Push signs in with the plugin's DevJock sign-in (the one `initialize-devjock` uses; it opens a browser the first time) and calls `POST https://api.devjock.com/v1.0/memories/update?memory_id=N`. If it is not available, it falls back to a token cached by mcp-remote. On 401 or no token: run `/devjock:reauthenticate`, then `push` again.

