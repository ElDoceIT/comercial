from fastapi import APIRouter, HTTPException, status

from app.core.config import get_settings
from app.services.drive_service import DriveService, DriveServiceError
from app.services.processing_service import ProcessingService, ProcessingServiceError


router = APIRouter(prefix="/drive", tags=["drive"])


@router.get("/health")
def drive_health() -> dict[str, str]:
    settings = get_settings()
    service = DriveService(settings)

    try:
        return service.validate_configuration()
    except DriveServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get("/files")
def list_drive_files() -> dict[str, object]:
    settings = get_settings()
    service = DriveService(settings)

    try:
        files = service.list_xls_files()
    except DriveServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return {
        "folder_id": settings.google_drive_input_folder_id,
        "count": len(files),
        "files": files,
    }


@router.get("/files/{file_id}/preview")
def preview_drive_file(file_id: str, rows: int = 5) -> dict[str, object]:
    settings = get_settings()
    service = DriveService(settings)

    try:
        preview = service.preview_xls_file(file_id=file_id, max_rows=min(rows, 10))
    except DriveServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return preview


@router.post("/download/{file_id}")
def download_drive_file(file_id: str) -> dict[str, str]:
    settings = get_settings()
    service = DriveService(settings)

    try:
        result = service.download_file(file_id=file_id)
    except DriveServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return result


@router.post("/inspect/{file_id}")
def inspect_drive_file_blocks(file_id: str) -> dict[str, object]:
    settings = get_settings()
    drive_service = DriveService(settings)
    processing_service = ProcessingService()

    try:
        download_result = drive_service.download_file(file_id=file_id)
        inspection_result = processing_service.inspect_xls_file(
            file_path=download_result["local_path"]
        )
    except DriveServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except ProcessingServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return {
        "status": "inspected",
        "download": download_result,
        "inspection": inspection_result,
    }
