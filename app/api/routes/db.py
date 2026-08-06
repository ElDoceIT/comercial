from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db


router = APIRouter(prefix="/db", tags=["db"])


@router.get("/health")
def db_health(db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        result = db.execute(text("SELECT 1 AS ok")).scalar()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo conectar a la base de datos.",
        ) from exc

    return {
        "message": "La conexion a la base de datos funciona correctamente.",
        "ok": result == 1,
    }
