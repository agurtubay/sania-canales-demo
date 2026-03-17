# app/channels/acs/sender.py
import asyncio
import os
from azure.communication.messages import NotificationMessagesClient
from azure.communication.messages.models import TextNotificationContent

_client = None

def _get_client() -> NotificationMessagesClient:
    global _client
    if _client is None:
        _client = NotificationMessagesClient.from_connection_string(
            os.environ["COMMUNICATION_SERVICES_CONNECTION_STRING"]
        )
    return _client

async def send_whatsapp_text(channel_id: str, to_phone: str, text: str):
    client = _get_client()
    msg = TextNotificationContent(
        channel_registration_id=channel_id,
        to=[to_phone],
        content=text,
    )
    return await asyncio.to_thread(client.send, msg)