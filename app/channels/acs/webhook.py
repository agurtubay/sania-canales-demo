# app/channels/acs/webhook.py
import json
from uuid import uuid4
from fastapi import Request
from ...core.types import InternalMessage
from ...core.agent import run_agent
from .sender import send_whatsapp_text

def _extract_text(data: dict) -> str:
    if data.get("content"):
        return data["content"]

    button = data.get("button") or {}
    if button.get("text"):
        return button["text"]
    if button.get("payload"):
        return button["payload"]

    interactive = data.get("interactive") or {}
    if interactive.get("type") == "buttonReply":
        btn = interactive.get("buttonReply") or {}
        return btn.get("title") or btn.get("id") or ""
    if interactive.get("type") == "listReply":
        item = interactive.get("listReply") or {}
        return item.get("title") or item.get("id") or ""

    return ""

async def handle_whatsapp_inbound(req: Request):
    events = await req.json()
    print("ACS_EVENTGRID_PAYLOAD=" + json.dumps(events, ensure_ascii=False))

    aeg_event_type = req.headers.get("aeg-event-type", "")

    if aeg_event_type == "SubscriptionValidation":
        code = events[0]["data"]["validationCode"]
        return {"validationResponse": code}

    for event in events:
        if event.get("eventType") != "Microsoft.Communication.AdvancedMessageReceived":
            continue

        data = event.get("data") or {}
        if data.get("channelType") != "whatsapp":
            continue

        user_id = data.get("from", "unknown")
        channel_id = data.get("to")
        text = _extract_text(data)
        if not text or not channel_id:
            continue

        msg = InternalMessage(
            channel="whatsapp",
            userId=user_id,
            conversationId=f"wa-{user_id}",
            correlationId=event.get("id", str(uuid4())),
            text=text,
        )

        resp = await run_agent(msg)
        await send_whatsapp_text(
            channel_id=channel_id,
            to_phone=user_id,
            text=resp.text,
        )

    return {"ok": True}