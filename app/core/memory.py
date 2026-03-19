"""Conversation history backed by Azure Cosmos DB (NoSQL)."""

import asyncio
import os
import time
from typing import Optional

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError

_client: Optional[CosmosClient] = None
_container = None

# Maximum conversation turns to keep (system msg + N user/assistant pairs)
MAX_TURNS = 20


def _get_container():
    global _client, _container
    if _container is not None:
        return _container

    conn_str = os.environ["COSMOS_CONNECTION_STRING"]
    db_name = os.getenv("COSMOS_DATABASE", "sania-bot")
    container_name = os.getenv("COSMOS_CONTAINER", "conversations")

    _client = CosmosClient.from_connection_string(conn_str)
    database = _client.get_database_client(db_name)
    _container = database.get_container_client(container_name)
    return _container


def _doc_id(conversation_id: str) -> str:
    """Document ID = conversation ID (one doc per conversation)."""
    return conversation_id


async def get_history(conversation_id: str) -> list[dict]:
    """Return the conversation history as a list of {role, content} dicts."""
    container = _get_container()
    doc_id = _doc_id(conversation_id)
    try:
        doc = await asyncio.to_thread(
            container.read_item, item=doc_id, partition_key=conversation_id
        )
        return doc.get("messages", [])
    except CosmosResourceNotFoundError:
        return []


async def append_turn(
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    channel: str = "whatsapp",
) -> None:
    """Append a user+assistant turn and persist to Cosmos DB."""
    container = _get_container()
    doc_id = _doc_id(conversation_id)

    try:
        doc = await asyncio.to_thread(
            container.read_item, item=doc_id, partition_key=conversation_id
        )
    except CosmosResourceNotFoundError:
        doc = {
            "id": doc_id,
            "conversationId": conversation_id,
            "channel": channel,
            "messages": [],
            "createdAt": time.time(),
        }

    messages = doc.get("messages", [])
    messages.append({"role": "user", "content": user_text})
    messages.append({"role": "assistant", "content": assistant_text})

    # Trim to last MAX_TURNS pairs (keep most recent context)
    if len(messages) > MAX_TURNS * 2:
        messages = messages[-(MAX_TURNS * 2):]

    doc["messages"] = messages
    doc["updatedAt"] = time.time()

    await asyncio.to_thread(container.upsert_item, doc)
