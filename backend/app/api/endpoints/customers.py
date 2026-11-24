from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, schemas
from app.database.connection import get_db
from sqlalchemy.orm import Session
from typing import List
from app.database import connection
from app.schemas import customer as customer_schema
from app.models import new_models as models

router = APIRouter()

@router.post("/", response_model=customer_schema.Customer)
def create_customer(customer: customer_schema.CustomerCreate, db: Session = Depends(connection.get_db)):
    db_customer = db.query(models.Customer).filter(models.Customer.email == customer.email).first()
    if db_customer:
        raise HTTPException(status_code=400, detail="Email already registered")
    new_customer = models.Customer(
        email=customer.email,
        first_name=customer.first_name,
        last_name=customer.last_name,
        phone_number=customer.phone_number,
        address=customer.address,
        whatsapp_number=customer.whatsapp_number,
        facebook_handle=customer.facebook_handle,
        instagram_handle=customer.instagram_handle,
        twitter_handle=customer.twitter_handle
    )
    db.add(new_customer)
    db.commit()
    db.refresh(new_customer)
    return new_customer

@router.get("/", response_model=list[schemas.customer.Customer])
def read_customers(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    customers = db.query(models.Customer).offset(skip).limit(limit).all()
    return customers

@router.get("/{customer_id}", response_model=customer_schema.Customer)
def read_customer(customer_id: str, db: Session = Depends(connection.get_db)):
    db_customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if db_customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return db_customer

@router.put("/{customer_id}", response_model=customer_schema.Customer)
def update_customer(customer_id: str, customer: customer_schema.CustomerCreate, db: Session = Depends(connection.get_db)):
    db_customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if db_customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    update_data = customer.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_customer, key, value)
    db.commit()
    db.refresh(db_customer)
    return db_customer

@router.delete("/{customer_id}", status_code=204)
def delete_customer(customer_id: str, db: Session = Depends(connection.get_db)):
    db_customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if db_customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    db.delete(db_customer)
    db.commit()
    return