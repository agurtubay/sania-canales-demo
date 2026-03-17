from pydantic import BaseModel

class InternalMessage(BaseModel):
    channel: str = "whatsapp"
    userId: str
    conversationId: str
    correlationId: str
    text: str

class InternalResponse(BaseModel):
    correlationId: str
    text: str