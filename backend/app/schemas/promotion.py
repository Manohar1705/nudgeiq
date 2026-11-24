from pydantic import BaseModel
import uuid
from datetime import datetime

class PromotionBase(BaseModel):
    name: str
    description: str | None = None
    discount_percentage: float | None = None

class PromotionCreate(PromotionBase):
    pass

class Promotion(PromotionBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True