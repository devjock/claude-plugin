#!/usr/bin/env python3
"""
Authorize DevJock API

Shared OAuth 2.0 authorization helper for any DevJock Python script that
calls the DevJock API. Uses PKCE flow via the MCP proxy server with
automatic token save/test/refresh/browser-login cascade.

Tokens are persisted in the OS-native credential store: the **macOS
Keychain** (generic-password item, service `devjock-oauth`, account
`devjock-sync`) on macOS/Linux via the `security` CLI, or the **Windows
Credential Manager** (generic credential, target `devjock-oauth:devjock-sync`)
on Windows via the Win32 Credential API (ctypes, stdlib only — no pywin32
dependency). The legacy file path `~/.claude/.devjock-sync-token.json` is
used as a one-time migration source if no credential-store entry exists yet,
then never written again.

Usage:
    from pathlib import Path
    import sys
    # Resolve the plugin root from the CALLING file's own location. Never
    # hardcode the marketplace folder name -- it depends on how the
    # marketplace was added (adding it from the GitHub repo produces a
    # different folder than adding it by its declared name). And never read
    # CLAUDE_PLUGIN_ROOT here: Claude Code substitutes that textually into
    # markdown, but does NOT export it into a Python subprocess, so
    # os.environ.get() returns None.
    #
    # parents[N] is the depth from the calling file UP to the plugin root --
    # count it per file. A script at skills/<name>/foo.py uses parents[2];
    # one at skills/<name>/lib/foo.py uses parents[3].
    _LIB = Path(__file__).resolve().parents[2] / "skills" / "lib"
    sys.path.insert(0, str(_LIB))
    from authorize_devjock_api import get_access_token, API_BASE

    token = get_access_token()
    # Use token in Authorization: Bearer {token} headers
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

__all__ = [
    "DEVJOCK_ENV",
    "get_access_token",
    "test_token",
    "API_BASE",
    "MCP_SERVER",
    "KEYCHAIN_SERVICE",
    "KEYCHAIN_ACCOUNT",
    "LEGACY_TOKEN_FILE",
    "OAUTH_SCOPES",
]

# --- Constants ---

# DEVJOCK_ENV=proto points every call at the proto stack, which runs php-api main,
# so plugin changes can be tested against server changes before they ship. Proto
# signs in through its own Clerk instance, so its tokens live under a separate
# credential account and never overwrite the production token.
DEVJOCK_ENV = (os.environ.get("DEVJOCK_ENV") or "production").strip().lower()
_ENVIRONMENTS = {
    "production": ("https://api.devjock.com/v1.0", "https://tasks-mcp.devjock.com", "devjock-sync"),
    "proto": ("https://api.devjockproto.com/v1.0", "https://tasks-mcp.devjockproto.com", "devjock-sync-proto"),
}
if DEVJOCK_ENV not in _ENVIRONMENTS:
    raise RuntimeError(f"DEVJOCK_ENV must be one of {sorted(_ENVIRONMENTS)}, got {DEVJOCK_ENV!r}")
API_BASE, MCP_SERVER, KEYCHAIN_ACCOUNT = _ENVIRONMENTS[DEVJOCK_ENV]
KEYCHAIN_SERVICE = "devjock-oauth"
# Legacy file kept for one-time migration only. New code never writes here.
LEGACY_TOKEN_FILE = Path.home() / ".claude" / ".devjock-sync-token.json"
OAUTH_SCOPES = "openid profile email offline_access"


# --- Public API ---

def get_access_token():
    """Get a valid access token: try saved token, refresh, or browser login.

    This is the main entry point. Call this to get a Bearer token for the
    DevJock API. It handles the full cascade automatically:
      1. Read saved token from macOS Keychain (one-time migration from the
         legacy file if needed)
      2. Test if it's still valid
      3. Try refreshing with refresh_token
      4. Fall back to browser OAuth login
    """
    saved = _load_tokens()

    if saved:
        # Try the saved access token
        if test_token(saved.get("access_token", "")):
            return saved["access_token"]

        # Try refreshing
        if "refresh_token" in saved and "client_id" in saved:
            try:
                token = _refresh_token(saved["refresh_token"], saved["client_id"])
                if test_token(token):
                    return token
            except Exception:
                pass

    # Fall back to browser login
    return _oauth_browser_login()


def test_token(token):
    """Check if a token is valid against the DevJock API.

    Makes a lightweight API call to verify the token works.
    Returns True if valid, False otherwise.
    """
    if not token:
        return False
    try:
        req = urllib.request.Request(
            f"{API_BASE}/agents/list?page=1&page_size=1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        urllib.request.urlopen(req)
        return True
    except Exception:
        return False


# --- Internal OAuth functions ---

def _pkce_pair():
    """Generate PKCE code_verifier and code_challenge."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _find_free_port():
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _register_client(redirect_uri):
    """Register a dynamic OAuth client with the MCP server."""
    payload = json.dumps({
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "DevJock Plugin Script",
        "token_endpoint_auth_method": "none",
    }).encode()

    req = urllib.request.Request(
        f"{MCP_SERVER}/register",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def _oauth_browser_login():
    """Open browser for DevJock login via MCP server OAuth proxy."""
    verifier, challenge = _pkce_pair()
    port = _find_free_port()
    redirect_uri = f"http://localhost:{port}/oauth/callback"
    state = secrets.token_urlsafe(32)

    # Register dynamic client
    client = _register_client(redirect_uri)
    client_id = client["client_id"]

    auth_url = (
        f"{MCP_SERVER}/authorize?"
        + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": OAUTH_SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
    )

    result = {"code": None, "error": None}

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("state", [None])[0] != state:
                result["error"] = "State mismatch"
            elif "error" in qs:
                result["error"] = qs["error"][0]
            else:
                result["code"] = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = "Login successful! You can close this tab." if result["code"] else f"Error: {result['error']}"
            self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

        def log_message(self, *args):
            pass  # suppress noisy logs

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 120

    print("  Opening browser for DevJock login...")
    print("  To sign in as a different DevJock account, paste this link into a private window instead:")
    print(f"  {auth_url}")
    webbrowser.open(auth_url)

    # Wait for callback
    while result["code"] is None and result["error"] is None:
        server.handle_request()
    server.server_close()

    if result["error"]:
        raise RuntimeError(f"OAuth error: {result['error']}")

    # Exchange code for tokens via MCP server
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }).encode()

    req = urllib.request.Request(
        f"{MCP_SERVER}/oauth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = urllib.request.urlopen(req)
    tokens = json.loads(resp.read())
    tokens["client_id"] = client_id  # save for refresh

    _save_tokens(tokens)
    return tokens["access_token"]


def _refresh_token(refresh_tok, client_id):
    """Try to refresh an expired access token."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_tok,
    }).encode()

    req = urllib.request.Request(
        f"{MCP_SERVER}/oauth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = urllib.request.urlopen(req)
    tokens = json.loads(resp.read())

    _save_tokens(tokens)
    return tokens["access_token"]


def _load_tokens():
    """Read token blob from the OS credential store. One-time migration from legacy file."""
    try:
        saved = _win_read_credential() if sys.platform == "win32" else _mac_read_keychain()
        if saved is not None:
            return saved
    except Exception:
        pass

    # One-time migration: legacy file → credential store. The legacy file only ever
    # held a production token, so it is never migrated into another environment.
    if DEVJOCK_ENV == "production" and LEGACY_TOKEN_FILE.exists():
        try:
            saved = json.loads(LEGACY_TOKEN_FILE.read_text())
            _save_tokens(saved)
            # Rename so we know it was migrated; don't delete in case of rollback
            backup = LEGACY_TOKEN_FILE.with_suffix(".json.migrated-to-keychain")
            try:
                LEGACY_TOKEN_FILE.rename(backup)
            except OSError:
                pass
            store_name = "Windows Credential Manager" if sys.platform == "win32" else "macOS Keychain"
            print(f"  ✓ Migrated DevJock OAuth tokens from {LEGACY_TOKEN_FILE.name} to {store_name}", file=sys.stderr)
            return saved
        except Exception:
            pass

    return None


def _save_tokens(tokens):
    """Persist token data in the OS credential store (overwrite any prior entry)."""
    blob = json.dumps(tokens)
    if sys.platform == "win32":
        _win_write_credential(blob)
    else:
        _mac_write_keychain(blob)


def _mac_read_keychain():
    """Read the token blob from the macOS Keychain, or None if absent/unreadable."""
    out = subprocess.run(
        ["security", "find-generic-password",
         "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode == 0 and out.stdout.strip():
        return json.loads(out.stdout.strip())
    return None


def _mac_write_keychain(blob):
    """Persist the token blob in the macOS Keychain (overwrite any prior entry)."""
    # Delete any existing entry first so add doesn't error on duplicate
    subprocess.run(
        ["security", "delete-generic-password",
         "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE],
        capture_output=True, check=False,
    )
    res = subprocess.run(
        ["security", "add-generic-password",
         "-a", KEYCHAIN_ACCOUNT,
         "-s", KEYCHAIN_SERVICE,
         "-l", "DevJock OAuth tokens (Clerk PKCE)",
         "-j", "Used by authorize_devjock_api.py for all DevJock REST API calls. Contains Clerk access_token + refresh_token + client_id.",
         "-w", blob],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to write DevJock tokens to Keychain: {res.stderr.strip()}")


# Windows Credential Manager target name for the generic credential.
WINDOWS_CRED_TARGET = f"{KEYCHAIN_SERVICE}:{KEYCHAIN_ACCOUNT}"


def _win_read_credential():
    """Read the token blob from Windows Credential Manager, or None if absent/unreadable."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    CRED_TYPE_GENERIC = 1

    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]

    cred_ptr = ctypes.POINTER(_CREDENTIAL)()
    if not advapi32.CredReadW(WINDOWS_CRED_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
        return None  # not found or unreadable — caller falls back to legacy file / login
    try:
        cred = cred_ptr.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return json.loads(blob.decode("utf-8"))
    finally:
        advapi32.CredFree(cred_ptr)


def _win_write_credential(blob):
    """Persist the token blob in Windows Credential Manager (overwrites any prior entry)."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL

    blob_bytes = blob.encode("utf-8")
    blob_buf = ctypes.create_string_buffer(blob_bytes, len(blob_bytes))

    cred = _CREDENTIAL()
    ctypes.memset(ctypes.byref(cred), 0, ctypes.sizeof(cred))
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = WINDOWS_CRED_TARGET
    cred.Comment = "DevJock OAuth tokens (Clerk PKCE) — access_token + refresh_token + client_id"
    cred.CredentialBlobSize = len(blob_bytes)
    cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_char))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = KEYCHAIN_ACCOUNT

    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        err = ctypes.get_last_error()
        raise RuntimeError(f"Failed to write DevJock tokens to Windows Credential Manager (WinError {err})")


# --- Self-test ---

if __name__ == "__main__":
    print("DevJock Auth Module — Self-Test")
    print("=" * 40)
    if sys.platform == "win32":
        print(f"Cred store: Windows Credential Manager / {WINDOWS_CRED_TARGET}")
    else:
        print(f"Cred store: macOS Keychain / {KEYCHAIN_SERVICE}/{KEYCHAIN_ACCOUNT}")
    print(f"Env:        {DEVJOCK_ENV}")
    print(f"API base:   {API_BASE}")
    print(f"MCP server: {MCP_SERVER}")
    print()

    token = get_access_token()
    # Show first/last 8 chars only
    masked = token[:8] + "..." + token[-8:] if len(token) > 20 else token
    print(f"Token:      {masked}")
    print(f"Valid:      {test_token(token)}")
    print()
    print("Auth module working.")
