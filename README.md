# DevJock for Claude

Connect Claude to your DevJock workspace — your tasks, memories, assistants and skills — through a secure DevJock sign-in. Nothing to install on your computer: no terminal, no Node.js, no Python.

When you say "Initialize DevJock", Claude loads your DevJock system prompts and the list of DevJock cloud skills and agents. In the **Code** tab (and in Claude Code) a small bundled script fetches them in one go before Claude reads anything. In the Cowork and Chat tabs, which have no shell, Claude fetches them one by one through the connector.

## What you need

- A DevJock account (the login you already use at devjock.ai).
- The Claude Desktop app, or Claude Code.

## Install in Claude Desktop

Use the **Code** tab. It loads the whole plugin, including its DevJock connector, and runs the loader for you. Cowork also loads the plugin but has no shell, so loading is slower; Chat loads the skill but not the connector (see "Using the Chat tab" below).

1. Open Claude Desktop and switch to the **Code** tab.
2. Open **Customize → Plugins → Add → Add marketplace**.
3. Enter `devjock/claude-plugin` and add it.
4. Install the **DevJock** plugin from that marketplace.
5. The first time Claude uses DevJock, a browser window opens asking you to sign in to DevJock. Sign in and approve. Move through the screens promptly — if you take too long, Claude stops waiting and you will need to try again.
6. Ask Claude: **"Initialize DevJock."** Claude loads DevJock's operating context and asks what you want to work on.

After that first time, DevJock initializes itself whenever you start a new session in the Code tab or in Claude Code; you only need to say "Initialize DevJock" again if you sign out.

## Install in Claude Code

```
/plugin marketplace add devjock/claude-plugin
/plugin install devjock@devjock
```

Restart Claude Code, then type `/mcp`, choose **devjock**, and pick **Authenticate** to sign in. Then ask Claude to "initialize DevJock".

## Using the Chat tab

The Chat tab loads the plugin's "Initialize DevJock" skill, but not the plugin's own connector. Add DevJock as a **connector** once and the skill works there too:

1. In Claude Desktop or at claude.ai, open **Customize → Connectors**.
2. Click **+**, then **Add custom connector**.
3. Enter `https://tasks-mcp.devjock.com/mcp/`, name it **DevJock**, save, and sign in with your DevJock account.

**On a company Team or Enterprise plan,** only your organization's **Owner** can add a custom connector. They do it once at claude.ai under **Organization settings → Connectors** (**Add** → **Custom** → **Web**, same address). After that, everyone in the organization can switch it on for themselves under **Customize → Connectors**.

## Troubleshooting

- **Claude says DevJock isn't connected.** Sign in again: in Claude Code type `/mcp` → **devjock** → **Authenticate**; in Claude Desktop reopen the DevJock connector and sign in.
- **The browser says "can't connect" partway through sign-in.** You took a little too long on the approval screens. Try again and move a bit faster.
- **"Initialize DevJock" says DevJock isn't connected in the Chat tab.** Chat doesn't use the plugin's connector. Add the DevJock connector (steps above), or switch to Cowork.

Questions: support@tradeloopcorp.com

## Testing against proto (DevJock staff)

Set `DEVJOCK_ENV=proto` to point the initialize skill at the proto stack, which runs the latest server code. Proto signs in through its own account system and stores its token separately, so your production sign-in is untouched.

```
DEVJOCK_ENV=proto python3 skills/lib/authorize_devjock_api.py
DEVJOCK_ENV=proto python3 skills/initialize-devjock/inject-system-prompts.py
```

The first command signs you in to proto in the browser. The second writes the system prompts your proto account receives to `skills/initialize-devjock/injected-context.md`. In Claude Code, start the session with `DEVJOCK_ENV=proto claude` to run the skill against proto.
