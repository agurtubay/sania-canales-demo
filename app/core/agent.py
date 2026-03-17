import asyncio
import os
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
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

async def run_agent(msg: InternalMessage) -> InternalResponse:
    client = _get_client()

    response = await asyncio.to_thread(
        client.responses.create,
        model=os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini"),
        input=[
            {
                "type": "message",
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": SYSTEM_PROMPT,
                    }
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": msg.text,
                    }
                ],
            },
        ],
    )

    return InternalResponse(
        correlationId=msg.correlationId,
        text=response.output_text,
    )