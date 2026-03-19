# app/channels/acs/sender.py
import asyncio
import os
from azure.communication.messages import NotificationMessagesClient
from azure.communication.messages.models import TextNotificationContent

_client = None

def _get_client() -> NotificationMessagesClient:
    global _client
    if _client is None:
        endpoint = os.environ["ACS_ENDPOINT"]
        access_key = os.environ["ACS_ACCESS_KEY"]
        conn_str = f"endpoint={endpoint};accesskey={access_key}"
        _client = NotificationMessagesClient.from_connection_string(conn_str)
    return _client

async def send_whatsapp_text(channel_id: str, to_phone: str, text: str):
    client = _get_client()
    msg = TextNotificationContent(
        channel_registration_id=channel_id,
        to=[to_phone],
        content=text,
    )
    return await asyncio.to_thread(client.send, msg)