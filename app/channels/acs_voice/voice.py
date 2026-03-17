import hashlib
import json
import os
from datetime import datetime, timezone
from fastapi import Request
from azure.core.exceptions import HttpResponseError
from azure.communication.callautomation import CallAutomationClient, TextSource

_client = None


def _voice_log(event: str, **fields):
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    print("VOICE_TRACE=" + json.dumps(payload, ensure_ascii=False, default=str))


def _get_env(*names: str, required: bool = True, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    if required:
        raise KeyError(names[0])
    return default


def get_call_client() -> CallAutomationClient:
    global _client
    if _client is None:
        endpoint = _get_env("ACS_ENDPOINT")
        access_key = _get_env("ACS_ACCESS_KEY", "COMMUNICATION_SERVICES_ACCESS_KEY")

        _voice_log(
            "client_config",
            endpoint=endpoint,
            key_len=len(access_key),
            key_fp=hashlib.sha256(access_key.encode("utf-8")).hexdigest()[:12],
            callback_base=_get_env("VOICE_CALLBACK_BASE_URL", "CALLBACK_BASE", required=False),
        )

        connection_string = f"endpoint={endpoint};accesskey={access_key}"
        _client = CallAutomationClient.from_connection_string(connection_string)
        _voice_log("client_created")
    return _client


async def handle_incoming_call(req: Request):
    events = await req.json()
    _voice_log(
        "incoming_request_received",
        aeg_event_type=req.headers.get("aeg-event-type"),
        event_count=len(events) if isinstance(events, list) else 1,
    )
    print("VOICE_EVENTGRID_PAYLOAD=" + json.dumps(events, ensure_ascii=False))

    if req.headers.get("aeg-event-type") == "SubscriptionValidation":
        code = events[0]["data"]["validationCode"]
        _voice_log("subscription_validation", validation_code=code)
        return {"validationResponse": code}

    client = get_call_client()
    callback_base = _get_env("VOICE_CALLBACK_BASE_URL", "CALLBACK_BASE").rstrip("/")
    cognitive_services_endpoint = _get_env("COGNITIVE_SERVICES_ENDPOINT", required=False)
    endpoint_normalized = (cognitive_services_endpoint or "").strip().rstrip("/")
    endpoint_for_acs = f"{endpoint_normalized}/" if endpoint_normalized else ""
    endpoint_lower = endpoint_normalized.lower()
    looks_like_speech_endpoint = bool(endpoint_normalized) and (
        endpoint_lower.endswith(".cognitiveservices.azure.com")
        or endpoint_lower.endswith(".api.cognitive.microsoft.com")
    )
    looks_like_foundry_endpoint = "openai.azure.com" in endpoint_lower or "foundry" in endpoint_lower

    _voice_log(
        "incoming_runtime_config",
        callback_base=callback_base,
        cognitive_services_endpoint=endpoint_normalized,
        cognitive_services_endpoint_sent=endpoint_for_acs,
        looks_like_speech_endpoint=looks_like_speech_endpoint,
        looks_like_foundry_endpoint=looks_like_foundry_endpoint,
    )

    if endpoint_normalized and not looks_like_speech_endpoint:
        _voice_log(
            "cognitive_endpoint_warning",
            configured_endpoint=endpoint_normalized,
            warning="Configured endpoint may not support ACS TTS/STT. Use Azure AI Speech endpoint.",
        )

    answered = 0

    for index, event in enumerate(events):
        event_type = event.get("eventType")
        _voice_log("incoming_event", index=index, event_type=event_type, event_id=event.get("id"))

        if event.get("eventType") != "Microsoft.Communication.IncomingCall":
            _voice_log("incoming_event_skipped", index=index, reason="event_type_not_incoming")
            continue

        data = event.get("data") or {}
        incoming_call_context = data.get("incomingCallContext")
        if not incoming_call_context:
            _voice_log("incoming_event_skipped", index=index, reason="missing_incoming_call_context")
            continue

        answer_kwargs = {
            "incoming_call_context": incoming_call_context,
            "callback_url": f"{callback_base}/channels/voice/callbacks",
        }
        if endpoint_for_acs:
            answer_kwargs["cognitive_services_endpoint"] = endpoint_for_acs

        _voice_log(
            "answer_call_attempt",
            index=index,
            callback_url=answer_kwargs["callback_url"],
            has_cognitive_endpoint=bool(endpoint_for_acs),
            incoming_call_context_fp=hashlib.sha256(incoming_call_context.encode("utf-8")).hexdigest()[:12],
        )

        try:
            answer_result = client.answer_call(**answer_kwargs)
            answered += 1
            _voice_log(
                "answer_call_success",
                index=index,
                call_connection_id=answer_result.call_connection_id,
                server_call_id=getattr(answer_result, "server_call_id", None),
            )
        except HttpResponseError as exc:
            _voice_log(
                "answer_call_error",
                index=index,
                status_code=exc.status_code,
                message=str(exc),
            )

    _voice_log("incoming_request_done", answered_count=answered)

    return {"ok": True}


async def handle_voice_callbacks(req: Request):
    events = await req.json()
    _voice_log(
        "callbacks_request_received",
        aeg_event_type=req.headers.get("aeg-event-type"),
        event_count=len(events) if isinstance(events, list) else 1,
    )
    print("VOICE_CALLBACKS_PAYLOAD=" + json.dumps(events, ensure_ascii=False))

    client = get_call_client()

    for index, event in enumerate(events):
        event_type = event.get("type") or event.get("eventType")
        data = event.get("data") or {}

        _voice_log(
            "callback_event",
            index=index,
            event_type=event_type,
            event_id=event.get("id"),
            call_connection_id=data.get("callConnectionId"),
            operation_context=data.get("operationContext"),
        )

        if event_type == "Microsoft.Communication.CallConnected":
            call_connection_id = data.get("callConnectionId")
            if not call_connection_id:
                _voice_log("callback_event_skipped", index=index, reason="missing_call_connection_id")
                continue
            call_conn = client.get_call_connection(call_connection_id)

            greeting = TextSource(
                text="Hola, soy SanIA. ¿En qué puedo ayudarte hoy?",
                source_locale="es-ES",
                voice_name="es-ES-ElviraNeural",
            )

            _voice_log(
                "play_media_attempt",
                index=index,
                call_connection_id=call_connection_id,
                source_locale="es-ES",
                voice_name="es-ES-ElviraNeural",
            )

            try:
                call_conn.play_media_to_all(
                    greeting,
                    operation_context="welcome-message",
                )
                _voice_log("play_media_success", index=index, call_connection_id=call_connection_id)
            except HttpResponseError as exc:
                _voice_log(
                    "play_media_error",
                    index=index,
                    status_code=exc.status_code,
                    message=str(exc),
                )

    _voice_log("callbacks_request_done")

    return {"ok": True}