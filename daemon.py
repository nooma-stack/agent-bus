"""
agent-bus — persistent Claude Code session daemon, bound to localhost.

Exposes POST /prompt that shells to `claude -p ... --session-id ...` so
remote clients can drive a fully-authenticated Claude session via a
single HTTP endpoint (typically reached through an SSH tunnel).

Auth: ~/.agent-bus/token holds a 256-bit random bearer token, generated
on first launch. All requests require `Authorization: Bearer <token>`.

Inherits Claude Code's keychain-stored OAuth credentials from the GUI
session that launches this daemon — must be started from a terminal on
the host's console (not from SSH), or from a LaunchAgent with
UserLoggedIn gating.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import uvicorn


TOKEN_DIR = Path.home() / ".agent-bus"
TOKEN_FILE = TOKEN_DIR / "token"
SESSIONS_FILE = TOKEN_DIR / "sessions.json"

DEFAULT_HOST = os.environ.get("AGENT_BUS_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("AGENT_BUS_PORT", "18900"))

# Never expose beyond loopback without a TLS terminator in front — the
# current auth scheme is a plaintext bearer token, which is fine over
# an SSH tunnel but not over the open network.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

logger = logging.getLogger("agent-bus")


def _ensure_token_dir() -> None:
    TOKEN_DIR.mkdir(mode=0o700, exist_ok=True)
    # Tighten even if the dir already existed with looser perms
    try:
        os.chmod(TOKEN_DIR, 0o700)
    except OSError:
        pass


def ensure_token() -> str:
    _ensure_token_dir()
    if not TOKEN_FILE.exists():
        token = secrets.token_urlsafe(32)
        TOKEN_FILE.write_text(token + "\n")
        TOKEN_FILE.chmod(0o600)
        logger.info("Generated new bearer token at %s", TOKEN_FILE)
        return token
    return TOKEN_FILE.read_text().strip()


def load_sessions() -> dict[str, dict]:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except json.JSONDecodeError:
            logger.warning("sessions.json was corrupt — starting fresh")
    return {}


def save_sessions(sessions: dict[str, dict]) -> None:
    _ensure_token_dir()
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))
    SESSIONS_FILE.chmod(0o600)


TOKEN = ensure_token()
SESSIONS: dict[str, dict] = load_sessions()


class PromptRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    # Optional: working dir the Claude subprocess runs in. Lets callers
    # scope file access without coordinating via env vars.
    cwd: Optional[str] = None
    # Optional: override which model / CLI flags to pass. Skipped in v1.


app = FastAPI(title="agent-bus", version="0.1.0")


def _check_auth(authorization: Optional[str]) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, TOKEN):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _run_claude(prompt: str, session_id: str, is_existing: bool, cwd: Optional[str]) -> dict:
    """Shell to the claude CLI. Returns the parsed response blob plus metadata."""
    flag = "--resume" if is_existing else "--session-id"
    cmd = [
        "claude", "-p", prompt,
        flag, session_id,
        "--output-format", "json",
    ]
    logger.info("spawn claude %s %s (cwd=%s)", flag, session_id, cwd or "inherit")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=600)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="claude subprocess timed out (600s)")

    # Claude with --output-format json emits a single JSON object on stdout.
    # Parse it to pull out the result text for convenience, but pass the
    # whole blob through so callers can inspect cost/model/etc.
    parsed: Optional[dict] = None
    text_result: Optional[str] = None
    if result.returncode == 0 and result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
            text_result = parsed.get("result") or parsed.get("message") or None
        except json.JSONDecodeError:
            parsed = None
            text_result = result.stdout

    return {
        "session_id": session_id,
        "result": text_result,
        "raw": parsed if parsed is not None else {"stdout": result.stdout},
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


@app.post("/prompt")
def prompt(req: PromptRequest, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)

    session_id = req.session_id or str(uuid.uuid4())
    # Defensive: reject non-UUID session IDs — claude --session-id requires UUID.
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="session_id must be a valid UUID")

    is_existing = session_id in SESSIONS
    response = _run_claude(req.prompt, session_id, is_existing, req.cwd)

    if response["exit_code"] == 0:
        SESSIONS[session_id] = {
            "last_prompt_at": int(__import__("time").time()),
            "messages": SESSIONS.get(session_id, {}).get("messages", 0) + 1,
        }
        save_sessions(SESSIONS)

    return response


@app.get("/health")
def health(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {
        "status": "ok",
        "sessions_known": len(SESSIONS),
        "version": "0.1.0",
    }


@app.delete("/sessions/{session_id}")
def forget_session(session_id: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    removed = SESSIONS.pop(session_id, None) is not None
    if removed:
        save_sessions(SESSIONS)
    return {"removed": removed, "session_id": session_id}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if DEFAULT_HOST not in _ALLOWED_HOSTS:
        logger.error(
            "refusing to bind %s — v1 is loopback-only. "
            "Expose via SSH tunnel, not direct network binding.",
            DEFAULT_HOST,
        )
        raise SystemExit(2)

    logger.info("agent-bus starting on %s:%s", DEFAULT_HOST, DEFAULT_PORT)
    logger.info("bearer token: %s (mode 0600)", TOKEN_FILE)
    logger.info("known sessions: %d", len(SESSIONS))
    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT, log_level="info")


if __name__ == "__main__":
    main()
