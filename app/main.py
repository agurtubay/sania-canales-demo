from fastapi import FastAPI, Request
from .core.types import InternalMessage
from .core.agent import run_agent
from .channels.acs.webhook import handle_whatsapp_inbound
from .channels.acs_voice.voice import handle_incoming_call, handle_voice_callbacks
import os, hashlib
from datetime import datetime, timezone
import json

app = FastAPI()


def _app_log(event: str, **fields):
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    print("APP_TRACE=" + json.dumps(payload, ensure_ascii=False, default=str))

print("VOICE_BUILD=2026-03-16-01")
for r in app.routes:
    print("ROUTE", getattr(r, "path", None), getattr(r, "methods", None))


@app.middleware("http")
async def request_trace(request: Request, call_next):
    if request.url.path.startswith("/channels/voice"):
        _app_log(
            "request_in",
            method=request.method,
            path=request.url.path,
            query=str(request.url.query),
            aeg_event_type=request.headers.get("aeg-event-type"),
            content_type=request.headers.get("content-type"),
        )
    response = await call_next(request)
    if request.url.path.startswith("/channels/voice"):
        _app_log(
            "request_out",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
        )
    return response

@app.get("/debug/ping")
async def debug_ping():
    return {"ok": True, "build": "2026-03-16-01"}

@app.get("/debug/voice-auth")
async def debug_voice_auth():
    endpoint = os.environ["ACS_ENDPOINT"].strip()
    key = (os.getenv("ACS_ACCESS_KEY") or os.getenv("COMMUNICATION_SERVICES_ACCESS_KEY") or "").strip()
    return {
        "endpoint": endpoint,
        "key_len": len(key),
        "key_fp": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
    }

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/core/message")
async def core_message(msg: InternalMessage):
    resp = await run_agent(msg)
    return resp.model_dump()

@app.post("/channels/whatsapp/inbound")
async def whatsapp_inbound(req: Request):
    return await handle_whatsapp_inbound(req)

@app.post("/channels/voice/incoming")
async def voice_incoming(req: Request):
    return await handle_incoming_call(req)

@app.post("/channels/voice/callbacks")
async def voice_callbacks(req: Request):
    return await handle_voice_callbacks(req)

