import asyncio
import os
import re
from typing import AsyncIterator
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from openai import Stream
from .types import InternalMessage, InternalResponse

SYSTEM_PROMPT = """\
Eres SanIA, un asistente virtual inteligente de Sanitas.

## Tu rol
Eres un asistente conversacional amable, profesional y empático que ayuda a los usuarios
con consultas generales relacionadas con salud, bienestar y servicios de Sanitas.

## Instrucciones
- Responde siempre en español, de manera clara, breve y útil.
- Sé empático y profesional en todo momento.
- Si el usuario tiene una emergencia médica, indícale que llame al 112 o acuda a urgencias de inmediato.
- No proporciones diagnósticos médicos. Puedes dar información general de salud, pero siempre recomienda consultar con un profesional médico para casos específicos.
- Si te falta información para responder, pregunta solo lo estrictamente necesario.
- Mantén un tono cercano pero profesional.
- Puedes usar emojis de forma moderada.
"""

VOICE_EXTRA_INSTRUCTIONS = """
- Esta conversación es por teléfono (voz). Responde de forma muy concisa, con frases cortas.
- NO uses emojis, asteriscos, markdown ni formato visual.
- Limita tu respuesta a 2-3 frases como máximo.
"""

_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF\U00002700-\U000027BF]+",
    flags=re.UNICODE,
)


def _strip_emojis(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


_SENTENCE_END_RE = re.compile(r'(?<=[.!?])\s+')

_client = None

def _get_client():
    global _client
    if _client is not None:
        return _client

    credential = (
        ManagedIdentityCredential()
        if os.getenv("WEBSITE_SITE_NAME")
        else DefaultAzureCredential()
    )

    project = AIProjectClient(
        endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        credential=credential,
    )
    _client = project.get_openai_client()
    return _client

def _build_input(msg: InternalMessage):
    system_text = SYSTEM_PROMPT
    if msg.channel == "voice":
        system_text += VOICE_EXTRA_INSTRUCTIONS
    return [
        {
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": system_text}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": msg.text}],
        },
    ]


async def run_agent(msg: InternalMessage) -> InternalResponse:
    client = _get_client()

    response = await asyncio.to_thread(
        client.responses.create,
        model=os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini"),
        input=_build_input(msg),
    )

    text = response.output_text
    if msg.channel == "voice":
        text = _strip_emojis(text)

    return InternalResponse(
        correlationId=msg.correlationId,
        text=text,
    )


async def run_agent_streaming(msg: InternalMessage) -> AsyncIterator[str]:
    """Stream agent response, yielding complete sentences as they arrive."""
    client = _get_client()

    stream: Stream = await asyncio.to_thread(
        client.responses.create,
        model=os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini"),
        input=_build_input(msg),
        stream=True,
    )

    buffer = ""
    for event in stream:
        if hasattr(event, "type") and event.type == "response.output_text.delta":
            buffer += event.delta
            # Split on sentence boundaries
            parts = _SENTENCE_END_RE.split(buffer)
            if len(parts) > 1:
                # Yield all complete sentences, keep the last (incomplete) part
                for sentence in parts[:-1]:
                    sentence = sentence.strip()
                    if sentence:
                        if msg.channel == "voice":
                            sentence = _strip_emojis(sentence)
                        if sentence:
                            yield sentence
                buffer = parts[-1]

    # Yield remaining buffer
    if buffer.strip():
        remaining = buffer.strip()
        if msg.channel == "voice":
            remaining = _strip_emojis(remaining)
        if remaining:
            yield remaining