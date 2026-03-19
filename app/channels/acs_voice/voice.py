import hashlib
import json
import os
from datetime import datetime, timezone
from fastapi import Request
from azure.core.exceptions import HttpResponseError
from azure.communication.callautomation import (
    CallAutomationClient,
    TextSource,
    RecognizeInputType,
    PhoneNumberIdentifier,
)
from ...core.agent import run_agent
from ...core.types import InternalMessage

_client = None
_SILENCE_TIMEOUT = 2
_call_callers: dict[str, str] = {}  # call_connection_id → caller phone number


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
        access_key = _get_env("ACS_ACCESS_KEY")

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

        caller_phone = (data.get("from") or {}).get("phoneNumber", {}).get("value", "")

        try:
            answer_result = client.answer_call(**answer_kwargs)
            answered += 1
            if caller_phone:
                _call_callers[answer_result.call_connection_id] = caller_phone
            _voice_log(
                "answer_call_success",
                index=index,
                call_connection_id=answer_result.call_connection_id,
                server_call_id=getattr(answer_result, "server_call_id", None),
                caller_phone=caller_phone,
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

        call_connection_id = data.get("callConnectionId")
        if not call_connection_id:
            _voice_log("callback_event_skipped", index=index, reason="missing_call_connection_id")
            continue

        call_conn = client.get_call_connection(call_connection_id)

        # ── CallConnected: play greeting + start listening (bundled) ──
        if event_type == "Microsoft.Communication.CallConnected":
            greeting = TextSource(
                text="Hola, soy SanIA. ¿En qué puedo ayudarte hoy?",
                source_locale="es-ES",
                voice_name="es-ES-ElviraNeural",
            )
            caller_phone = _call_callers.get(call_connection_id, "")
            _voice_log(
                "welcome_recognize_attempt",
                index=index,
                call_connection_id=call_connection_id,
                caller_phone=caller_phone,
            )
            if caller_phone:
                target = PhoneNumberIdentifier(caller_phone)
                try:
                    call_conn.start_recognizing_media(
                        input_type=RecognizeInputType.SPEECH,
                        target_participant=target,
                        play_prompt=greeting,
                        speech_language="es-ES",
                        end_silence_timeout=_SILENCE_TIMEOUT,
                    )
                    _voice_log("welcome_recognize_started", index=index, call_connection_id=call_connection_id)
                except HttpResponseError as exc:
                    _voice_log(
                        "welcome_recognize_error",
                        index=index,
                        status_code=exc.status_code,
                        message=str(exc),
                    )
            else:
                _voice_log("welcome_no_caller_phone", index=index, call_connection_id=call_connection_id)

        # ── PlayCompleted: no-op (recognition is bundled with play) ──
        elif event_type == "Microsoft.Communication.PlayCompleted":
            _voice_log("play_completed", index=index, call_connection_id=call_connection_id,
                        operation_context=data.get("operationContext"))

        # ── RecognizeCompleted: send speech to agent, play response ──
        elif event_type == "Microsoft.Communication.RecognizeCompleted":
            result_info = data.get("resultInformation", {})
            recognize_result = data.get("recognitionType")
            speech_result = data.get("speechResult", {})
            recognized_text = speech_result.get("speech", "")

            _voice_log(
                "recognize_completed",
                index=index,
                call_connection_id=call_connection_id,
                recognition_type=recognize_result,
                speech_text=recognized_text,
                result_code=result_info.get("subCode"),
            )

            if recognized_text.strip():
                await _handle_user_speech(call_conn, call_connection_id, recognized_text, index)
            else:
                _voice_log("recognize_empty_speech", index=index, call_connection_id=call_connection_id)
                _start_speech_recognition(call_conn, call_connection_id, index)

        # ── RecognizeFailed: check reason, re-listen or hang up ──
        elif event_type == "Microsoft.Communication.RecognizeFailed":
            result_info = data.get("resultInformation", {})
            sub_code = result_info.get("subCode", 0)
            msg = result_info.get("message", "")

            _voice_log(
                "recognize_failed",
                index=index,
                call_connection_id=call_connection_id,
                sub_code=sub_code,
                message=msg,
            )

            # 8510 = silence timeout — ask the user if they're still there
            if sub_code == 8510:
                caller_phone = _call_callers.get(call_connection_id, "")
                if caller_phone:
                    try:
                        prompt = TextSource(
                            text="¿Sigues ahí? Si necesitas algo más, no dudes en preguntarme.",
                            source_locale="es-ES",
                            voice_name="es-ES-ElviraNeural",
                        )
                        target = PhoneNumberIdentifier(caller_phone)
                        call_conn.start_recognizing_media(
                            input_type=RecognizeInputType.SPEECH,
                            target_participant=target,
                            play_prompt=prompt,
                            speech_language="es-ES",
                            end_silence_timeout=_SILENCE_TIMEOUT,
                            operation_context="silence-prompt-listen",
                        )
                        _voice_log("silence_prompt_recognize_started", index=index, call_connection_id=call_connection_id)
                    except HttpResponseError as exc:
                        _voice_log("silence_prompt_error", index=index, message=str(exc))
                else:
                    _voice_log("silence_prompt_no_caller", index=index, call_connection_id=call_connection_id)
            else:
                # Other failure — try listening again
                _start_speech_recognition(call_conn, call_connection_id, index)

        # ── CallDisconnected: cleanup ──
        elif event_type == "Microsoft.Communication.CallDisconnected":
            _call_callers.pop(call_connection_id, None)
            _voice_log("call_disconnected", index=index, call_connection_id=call_connection_id)

    _voice_log("callbacks_request_done")

    return {"ok": True}


def _start_speech_recognition(call_conn, call_connection_id: str, index: int):
    """Start listening for caller speech."""
    caller_phone = _call_callers.get(call_connection_id, "")
    _voice_log("recognize_start", index=index, call_connection_id=call_connection_id, caller_phone=caller_phone)

    if not caller_phone:
        _voice_log("recognize_start_error", index=index, message="No caller phone stored for this connection")
        return

    target = PhoneNumberIdentifier(caller_phone)
    try:
        call_conn.start_recognizing_media(
            input_type=RecognizeInputType.SPEECH,
            target_participant=target,
            speech_language="es-ES",
            end_silence_timeout=_SILENCE_TIMEOUT,
        )
        _voice_log("recognize_started", index=index, call_connection_id=call_connection_id)
    except HttpResponseError as exc:
        _voice_log(
            "recognize_start_error",
            index=index,
            status_code=exc.status_code,
            message=str(exc),
        )


async def _handle_user_speech(call_conn, call_connection_id: str, text: str, index: int):
    """Send recognized text to the agent, then play the response back."""
    _voice_log(
        "agent_request",
        index=index,
        call_connection_id=call_connection_id,
        user_text=text,
    )

    try:
        caller_phone = _call_callers.get(call_connection_id, "unknown")
        msg = InternalMessage(
            channel="voice",
            userId=caller_phone,
            conversationId=f"voice-{caller_phone}",
            correlationId=call_connection_id,
            text=text,
        )
        agent_response = await run_agent(msg)
        agent_text = agent_response.text

        _voice_log(
            "agent_response",
            index=index,
            call_connection_id=call_connection_id,
            response_text=agent_text[:500],
            response_len=len(agent_text),
        )

        caller_phone = _call_callers.get(call_connection_id, "")
        if caller_phone:
            response_source = TextSource(
                text=agent_text,
                source_locale="es-ES",
                voice_name="es-ES-ElviraNeural",
            )
            target = PhoneNumberIdentifier(caller_phone)
            call_conn.start_recognizing_media(
                input_type=RecognizeInputType.SPEECH,
                target_participant=target,
                play_prompt=response_source,
                speech_language="es-ES",
                end_silence_timeout=_SILENCE_TIMEOUT,
                operation_context="agent-response-listen",
            )
            _voice_log("agent_response_recognize_started", index=index, call_connection_id=call_connection_id)
        else:
            _voice_log("agent_response_no_caller", index=index, call_connection_id=call_connection_id)

    except Exception as exc:
        _voice_log(
            "agent_error",
            index=index,
            call_connection_id=call_connection_id,
            error=str(exc),
        )
        try:
            error_msg = TextSource(
                text="Lo siento, ha ocurrido un error. ¿Puedes repetirme tu pregunta?",
                source_locale="es-ES",
                voice_name="es-ES-ElviraNeural",
            )
            call_conn.play_media_to_all(error_msg, operation_context="error-message")
        except HttpResponseError:
            pass