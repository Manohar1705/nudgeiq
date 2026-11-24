from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.database import connection
from app.schemas import brand as brand_schema
from app.models import new_models as models

router = APIRouter()

@router.post("/", response_model=brand_schema.Brand)
def create_brand(brand: brand_schema.BrandCreate, db: Session = Depends(connection.get_db)):
    new_brand = models.Brand(**brand.dict())
    db.add(new_brand)
    db.commit()
    db.refresh(new_brand)
    return new_brand

@router.get("/", response_model=List[brand_schema.Brand])
def read_brands(skip: int = 0, limit: int = 100, db: Session = Depends(connection.get_db)):
    brands = db.query(models.Brand).offset(skip).limit(limit).all()
    return brands

@router.get("/{brand_id}", response_model=brand_schema.Brand)
def read_brand(brand_id: str, db: Session = Depends(connection.get_db)):
    db_brand = db.query(models.Brand).filter(models.Brand.id == brand_id).first()
    if db_brand is None:
        raise HTTPException(status_code=404, detail="Brand not found")
    return db_brand

@router.put("/{brand_id}", response_model=brand_schema.Brand)
def update_brand(brand_id: str, brand: brand_schema.BrandCreate, db: Session = Depends(connection.get_db)):
    db_brand = db.query(models.Brand).filter(models.Brand.id == brand_id).first()
    if db_brand is None:
        raise HTTPException(status_code=404, detail="Brand not found")
    for var, value in vars(brand).items():
        setattr(db_brand, var, value) if value else None
    db.commit()
    db.refresh(db_brand)
    return db_brand

@router.delete("/{brand_id}", status_code=204)
def delete_brand(brand_id: str, db: Session = Depends(connection.get_db)):
    db_brand = db.query(models.Brand).filter(models.Brand.id == brand_id).first()
    if db_brand is None:
        raise HTTPException(status_code=404, detail="Brand not found")
    db.delete(db_brand)
    db.commit()
    return