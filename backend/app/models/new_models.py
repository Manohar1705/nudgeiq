import uuid
from sqlalchemy import Column, String, DateTime, Boolean, Float, ForeignKey, Text, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class Customer(Base):
    __tablename__ = 'customers'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    phone_number = Column(String, nullable=True)
    address = Column(String, nullable=True)
    whatsapp_number = Column(String, nullable=True)
    facebook_handle = Column(String, nullable=True)
    instagram_handle = Column(String, nullable=True)
    twitter_handle = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    preferences = relationship("CustomerPreferences", back_populates="customer", uselist=False)
    consents = relationship("CustomerConsents", back_populates="customer", uselist=False)
    purchases = relationship("Purchase", back_populates="customer")
    events = relationship("CustomerEvent", back_populates="customer")

class CustomerPreferences(Base):
    __tablename__ = 'customer_preferences'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id = Column(UUID(as_uuid=True), ForeignKey('customers.id'), nullable=False)
    interests = Column(Text) # e.g., '{"categories": ["electronics", "books"]}'
    communication_channel = Column(String) # e.g., 'email', 'whatsapp'
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="preferences")

class CustomerConsents(Base):
    __tablename__ = 'customer_consents'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id = Column(UUID(as_uuid=True), ForeignKey('customers.id'), nullable=False)
    email_marketing = Column(Boolean, default=False)
    whatsapp_marketing = Column(Boolean, default=False)
    instagram_marketing = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="consents")

class Brand(Base):
    __tablename__ = 'brands'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    products = relationship("Product", back_populates="brand")
    campaigns = relationship("Campaign", back_populates="brand")

class Product(Base):
    __tablename__ = 'products'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id = Column(UUID(as_uuid=True), ForeignKey('brands.id'), nullable=False)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    brand = relationship("Brand", back_populates="products")

class Promotion(Base):
    __tablename__ = 'promotions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(Text)
    discount_percentage = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Campaign(Base):
    __tablename__ = 'campaigns'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id = Column(UUID(as_uuid=True), ForeignKey('brands.id'), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey('products.id'), nullable=False)
    promotion_id = Column(UUID(as_uuid=True), ForeignKey('promotions.id'))
    name = Column(String, nullable=False)
    description = Column(Text)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    budget = Column(Float)
    max_audience = Column(Integer)
    status = Column(String, default='draft') # e.g., draft, active, completed, paused
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    brand = relationship("Brand", back_populates="campaigns")
    product = relationship("Product")
    promotion = relationship("Promotion")
    decisions = relationship("CampaignDecision", back_populates="campaign")

class CampaignDecision(Base):
    __tablename__ = 'campaign_decisions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id = Column(UUID(as_uuid=True), ForeignKey('campaigns.id'), nullable=False)
    customer_id = Column(UUID(as_uuid=True), ForeignKey('customers.id'), nullable=False)
    model_version_id = Column(UUID(as_uuid=True), ForeignKey('model_versions.id'))
    relevance_score = Column(Float)
    selection_reason = Column(String)
    action = Column(String) # e.g., 'email', 'whatsapp'
    timing = Column(String) # e.g., 'morning', 'evening'
    created_at = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("Campaign", back_populates="decisions")
    customer = relationship("Customer")
    model_version = relationship("ModelVersion")
    message = relationship("Message", back_populates="decision", uselist=False)
    reward = relationship("Reward", back_populates="decision", uselist=False)

class Message(Base):
    __tablename__ = 'messages'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_id = Column(UUID(as_uuid=True), ForeignKey('campaign_decisions.id'), nullable=False)
    channel = Column(String, nullable=False) # e.g., 'email', 'whatsapp'
    content = Column(Text)
    status = Column(String) # e.g., 'sent', 'delivered', 'failed'
    sent_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    decision = relationship("CampaignDecision", back_populates="message")

class CustomerEvent(Base):
    __tablename__ = 'customer_events'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id = Column(UUID(as_uuid=True), ForeignKey('customers.id'), nullable=False)
    campaign_id = Column(UUID(as_uuid=True), ForeignKey('campaigns.id'))
    decision_id = Column(UUID(as_uuid=True), ForeignKey('campaign_decisions.id'))
    event_type = Column(String, nullable=False) # e.g., 'purchase', 'click', 'open'
    event_data = Column(Text) # e.g., '{"product_id": "...'}
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="events")
    campaign = relationship("Campaign")
    decision = relationship("CampaignDecision")

class Purchase(Base):
    __tablename__ = 'purchases'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id = Column(UUID(as_uuid=True), ForeignKey('customers.id'), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey('products.id'), nullable=False)
    campaign_id = Column(UUID(as_uuid=True), ForeignKey('campaigns.id'))
    purchase_date = Column(DateTime, default=datetime.utcnow)
    purchase_amount = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="purchases")
    product = relationship("Product")
    campaign = relationship("Campaign")

class Reward(Base):
    __tablename__ = 'rewards'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_id = Column(UUID(as_uuid=True), ForeignKey('campaign_decisions.id'), nullable=False)
    reward_value = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    decision = relationship("CampaignDecision", back_populates="reward")

class ModelVersion(Base):
    __tablename__ = 'model_versions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_name = Column(String, nullable=False)
    version = Column(String, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

class Experiment(Base):
    __tablename__ = 'experiments'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    model_version_id = Column(UUID(as_uuid=True), ForeignKey('model_versions.id'), nullable=False)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    model_version = relationship("ModelVersion")