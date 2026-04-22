# agent-bus

Persistent Claude Code session daemon — exposes a running, authenticated
`claude` CLI as a local HTTP endpoint so remote clients (other agents,
automation scripts, IDE plugins on other machines) can drive it through
an SSH tunnel without re-authing each call.

## What it is

- **Small** FastAPI daemon, one Python file (~200 lines).
- **Token-authenticated** — first launch generates a 256-bit bearer
  token at `~/.agent-bus/token`.
- **Loopback-bound** — listens on `127.0.0.1:18900`. Access beyond the
  host is expected to go over SSH tunnel, not open network exposure.
- **Keychain-inheriting** — must be launched from a GUI-authenticated
  session (or a LaunchAgent gated on `Aqua` session type). That's how
  it gets access to Claude Code's OAuth token.
- **Session-preserving** — routes each prompt to
  `claude -p --session-id <uuid> --output-format json`, so multiple
  calls with the same `session_id` share conversation context.

## What it isn't

- Not a multi-user server (v1 is for a single operator's Claude Code).
- Not a proxy for API-key-based billing (it uses the Claude subscription
  the CLI is logged into).
- Not yet a streaming interface — v1 is synchronous request/response.

## Install (macOS)

```
git clone https://github.com/nooma-stack/agent-bus.git ~/agent-bus
cd ~/agent-bus
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run (manual, foreground)

From a **Mac Studio / Mac mini Terminal window on the console**, not
over SSH — the daemon needs keychain access:

```
cd ~/agent-bus
.venv/bin/python daemon.py
```

On first start the daemon writes a bearer token to
`~/.agent-bus/token` (mode 0600). Output will include the path.

## Run (LaunchAgent)

For always-on use, install the LaunchAgent template:

```
mkdir -p ~/Library/LaunchAgents
sed -e "s|PLACEHOLDER_HOME|$HOME|g" \
    -e "s|PLACEHOLDER_PYTHON|$HOME/agent-bus/.venv/bin/python|g" \
    com.nooma.agent-bus.plist.template \
  > ~/Library/LaunchAgents/com.nooma.agent-bus.plist
launchctl load -w ~/Library/LaunchAgents/com.nooma.agent-bus.plist
```

The agent is gated on `LimitLoadToSessionType=Aqua`, so it only runs
when a user is logged into the GUI — that's what gives it keychain
access.

Logs: `~/.agent-bus/daemon.log`

## Use (remote client)

From another machine, open an SSH tunnel to the daemon:

```
ssh -L 18900:127.0.0.1:18900 -N -f <remote-host>
```

Fetch the token (one time):

```
scp <remote-host>:.agent-bus/token ~/.agent-bus/remote-token
```

Send a prompt:

```
curl -s http://127.0.0.1:18900/prompt \
  -H "Authorization: Bearer $(cat ~/.agent-bus/remote-token)" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Read the file README.md and tell me what it says in one line"}'
```

Response:

```json
{
  "session_id": "c8d5b7...",
  "result": "The README describes a persistent Claude Code session daemon...",
  "raw": { "...": "..." },
  "stderr": "",
  "exit_code": 0
}
```

Subsequent prompts with the same `session_id` share conversation context
(Claude resumes the session via `--resume`).

## API

```
POST /prompt
  Authorization: Bearer <token>
  Body: {"prompt": "...", "session_id": "<optional uuid>", "cwd": "<optional path>"}
  Returns: {session_id, result, raw, stderr, exit_code}

GET /health
  Authorization: Bearer <token>
  Returns: {status, sessions_known, version}

DELETE /sessions/{session_id}
  Authorization: Bearer <token>
  Returns: {removed, session_id}
```

## Security model

- Token stored at `~/.agent-bus/token`, mode 0600 (user read/write only).
- Token verified via `secrets.compare_digest` (constant-time) on every
  request.
- Bind refuses non-loopback hosts. If you need network access, put a
  TLS-terminating reverse proxy (nginx, Caddy, Tailscale Funnel) in
  front, and only then flip the bind via `AGENT_BUS_HOST`.
- To rotate the token: `rm ~/.agent-bus/token && restart daemon`, then
  re-fetch it on any client that trusts this daemon.

## Limitations (v1)

- Synchronous only — long prompts block the HTTP request until Claude
  returns. 600-second default timeout.
- Single daemon per host — no multi-tenant mode.
- No per-session quota or rate limiting.
- Session history stored by Claude Code itself in
  `~/.claude/projects/*/sessions/`. The daemon just tracks IDs, not
  message content.

## Roadmap

- v1 (this release): synchronous, token auth, loopback bind.
- v2: streaming responses (SSE), concurrent sessions, optional mTLS.
- v3: multi-daemon federation, token rotation with overlap, audit log.

## License

MIT
