from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.processing_service import (
    DuplicateArchivoIngestaError,
    ProcessingService,
    ProcessingServiceError,
)


router = APIRouter(prefix="/processing", tags=["processing"])


class PreviewCronogramasRequest(BaseModel):
    local_path: str
    archivo_ingesta_id: int | None = None


class LoadToDbRequest(BaseModel):
    file_id: str | None = None
    local_path: str | None = None


@router.post("/preview-cronogramas")
def preview_local_file_cronogramas(
    payload: PreviewCronogramasRequest,
) -> dict[str, object]:
    service = ProcessingService()

    try:
        dataframe = service.build_cronogramas_dataframe(
            file_path=payload.local_path,
            archivo_ingesta_id=payload.archivo_ingesta_id,
        )
    except ProcessingServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    preview_records = dataframe.head(10).to_dict(orient="records")

    return {
        "status": "preview",
        "file_path": payload.local_path,
        "total_rows": len(dataframe),
        "preview_count": len(preview_records),
        "columns": list(dataframe.columns),
        "rows": preview_records,
    }


@router.post("/load-to-db")
def load_monitor_file_to_db(
    payload: LoadToDbRequest, db: Session = Depends(get_db)
) -> dict[str, object]:
    service = ProcessingService()

    try:
        return service.load_monitor_file_to_db(
            db=db,
            file_id=payload.file_id,
            local_path=payload.local_path,
        )
    except DuplicateArchivoIngestaError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ProcessingServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
