---
name: reauthenticate
description: Re-authenticate the DevJock API (refresh or browser-login the OAuth token stored in the macOS Keychain)
---

# Re-authenticate DevJock

Establish or refresh the DevJock OAuth token that every DevJock Python helper uses to reach the API — the init prompt injector, the knowledge-graph loader, qa-tester, sdlc-audit, and others. Tokens are cached in the macOS Keychain (service `devjock-oauth`, account `devjock-sync`). This runs the save → test → refresh → browser-login cascade and reports status.

Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/lib/authorize_devjock_api.py
```

- If a valid token is already cached, it prints a masked token and `Valid: True` — nothing else to do.
- If the token is missing or expired, it opens a browser for DevJock login, saves the new token to the Keychain, and re-tests.
- This ONLY manages the auth token. It does NOT sync or regenerate local agent `.md` files — that legacy "sync-plugin" path was retired 2026-06-12.
