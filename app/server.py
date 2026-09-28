import copy
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # before importing modules that read env at import time

import anthropic  # noqa: E402
from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import data, harness, llm  # noqa: E402

app = FastAPI(title="Insurance Claims SOP Agent")
log = logging.getLogger("sop-agent")
SESSIONS = {}  # ponytail: in-memory, single process; swap for Redis if this ever runs multi-worker


class NewSession(BaseModel):
    consent_scenario: str = "default"


class ChatIn(BaseModel):
    session_id: str
    message: str


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/config")
def config():
    return {"model": llm.MODEL, "demo_today": str(data.today()),
            "consent_scenarios": list(data.CONSENT_SCENARIOS),
            "server_key": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))}


@app.post("/api/session")
def new_session(body: NewSession):
    if body.consent_scenario not in data.CONSENT_SCENARIOS:
        raise HTTPException(400, "unknown consent scenario")
    s = harness.Session(consent_scenario=body.consent_scenario)
    SESSIONS[s.id] = s
    return {"session_id": s.id, "reply": s.history[0]["content"], "state": harness.public_state(s)}


@app.post("/api/chat")
def chat(body: ChatIn, x_api_key: str | None = Header(None)):
    s = SESSIONS.get(body.session_id)
    if not s:
        raise HTTPException(404, "session not found; start a new conversation")
    message = body.message.strip()[:2000]
    if not message:
        raise HTTPException(400, "empty message")
    snapshot = copy.deepcopy(s)
    try:
        reply = harness.turn(s, message, llm.LLM(api_key=x_api_key or None))
    except Exception as e:
        SESSIONS[s.id] = snapshot  # a failed turn leaves nothing behind: no half-applied memory, attempts or history
        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            raise HTTPException(401, "The model API rejected the credentials. Enter a valid API key.")
        if isinstance(e, anthropic.CredentialsError) or (isinstance(e, TypeError) and "authentication" in str(e)):
            raise HTTPException(401, "No API credentials configured. Enter an API key or set ANTHROPIC_API_KEY.")
        if isinstance(e, anthropic.APIError):
            raise HTTPException(502, f"Model API error: {getattr(e, 'message', e)}")
        log.exception("turn failed for session %s", s.id)
        raise HTTPException(500, "Something went wrong on our side; your last message was not applied. Please try again.")
    return {"reply": reply, "state": harness.public_state(s)}
