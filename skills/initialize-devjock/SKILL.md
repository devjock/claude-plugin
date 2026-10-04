---
name: initialize-devjock
description: Start a DevJock session. Loads your DevJock system prompts and the DevJock skill/agent registry so Claude knows how DevJock works before you start. Use when the user says "initialize DevJock", "start DevJock", "load DevJock", or opens a conversation about their DevJock workspace, tasks, memories or assistants.
---

<!--
  - The !` line below runs BEFORE the model reads this file. Claude Code executes it,
    replaces the line with its output, then hands the file to the model. The model cannot skip it.
  - It signs in as you, fetches your DevJock system prompts, the registry and the session
    instructions, writes them to one local file, and prints that file's path.
  - It prints the path, not the content: Claude Code truncates command output over a few KB.
  - DevJock decides which prompts your account gets. Everything the model does next is written
    in DevJock (prompt 776), not here.
  - In the Cowork and Chat tabs there is no shell; the line shows as text and the last bullet applies. Use the Code tab.
-->

!`python3 "${CLAUDE_PLUGIN_ROOT}/skills/initialize-devjock/inject-system-prompts.py"`

- File path above → Read it to its last line (page by offset) and follow the session instructions at its end.
- Authentication failure above → run `/devjock:reauthenticate`, then run this skill again.
- No shell → `read_single_prompt(prompt_id=776)` via the DevJock connector and follow it. If the connector is missing or asks you to sign in, say: "DevJock isn't connected yet. Open the DevJock connector and sign in with your DevJock account, then ask me again."
