from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.database import connection
from app.schemas import promotion as promotion_schema
from app.models import new_models as models

router = APIRouter()

@router.post("/", response_model=promotion_schema.Promotion)
def create_promotion(promotion: promotion_schema.PromotionCreate, db: Session = Depends(connection.get_db)):
    new_promotion = models.Promotion(**promotion.dict())
    db.add(new_promotion)
    db.commit()
    db.refresh(new_promotion)
    return new_promotion

@router.get("/", response_model=List[promotion_schema.Promotion])
def read_promotions(skip: int = 0, limit: int = 100, db: Session = Depends(connection.get_db)):
    promotions = db.query(models.Promotion).offset(skip).limit(limit).all()
    return promotions

@router.get("/{promotion_id}", response_model=promotion_schema.Promotion)
def read_promotion(promotion_id: str, db: Session = Depends(connection.get_db)):
    db_promotion = db.query(models.Promotion).filter(models.Promotion.id == promotion_id).first()
    if db_promotion is None:
        raise HTTPException(status_code=404, detail="Promotion not found")
    return db_promotion

@router.put("/{promotion_id}", response_model=promotion_schema.Promotion)
def update_promotion(promotion_id: str, promotion: promotion_schema.PromotionCreate, db: Session = Depends(connection.get_db)):
    db_promotion = db.query(models.Promotion).filter(models.Promotion.id == promotion_id).first()
    if db_promotion is None:
        raise HTTPException(status_code=404, detail="Promotion not found")
    for var, value in vars(promotion).items():
        setattr(db_promotion, var, value) if value else None
    db.commit()
    db.refresh(db_promotion)
    return db_promotion

@router.delete("/{promotion_id}", status_code=204)
def delete_promotion(promotion_id: str, db: Session = Depends(connection.get_db)):
    db_promotion = db.query(models.Promotion).filter(models.Promotion.id == promotion_id).first()
    if db_promotion is None:
        raise HTTPException(status_code=404, detail="Promotion not found")
    db.delete(db_promotion)
    db.commit()
    return