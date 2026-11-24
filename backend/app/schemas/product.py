from pydantic import BaseModel
import uuid
from datetime import datetime

class ProductBase(BaseModel):
    name: str
    category: str
    price: float
    brand_id: uuid.UUID

class ProductCreate(ProductBase):
    pass

class Product(ProductBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True