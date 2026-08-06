from io import BytesIO
from pathlib import Path

from google.auth.exceptions import DefaultCredentialsError
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
import xlrd

from app.core.config import Settings


class DriveServiceError(Exception):
    """Error controlado para fallas de configuracion o acceso a Google Drive."""


class DriveService:
    """Encapsula el cliente de Google Drive para facilitar futuras extensiones."""

    DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    def is_configured(self) -> bool:
        return bool(
            self.settings.google_service_account_file
            and self.settings.google_drive_input_folder_id
        )

    def get_health_status(self) -> dict[str, bool | str | None]:
        credentials_path = self.settings.google_service_account_file

        return {
            "configured": self.is_configured(),
            "service_account_file": credentials_path,
            "service_account_file_exists": bool(
                credentials_path and Path(credentials_path).is_file()
            ),
            "input_folder_id": self.settings.google_drive_input_folder_id,
        }

    def validate_configuration(self) -> dict[str, str]:
        """Valida configuracion base y credenciales antes de operar con Drive."""

        folder_id = self.settings.google_drive_input_folder_id
        if not folder_id:
            raise DriveServiceError(
                "Falta la variable GOOGLE_DRIVE_INPUT_FOLDER_ID en la configuracion."
            )

        self._get_client()

        return {
            "message": "La configuracion de Google Drive esta cargada correctamente."
        }

    def list_xls_files(self) -> list[dict[str, str | list[str] | None]]:
        folder_id = self.settings.google_drive_input_folder_id
        if not folder_id:
            raise DriveServiceError(
                "Falta la variable GOOGLE_DRIVE_INPUT_FOLDER_ID en la configuracion."
            )

        client = self._get_client()
        query = (
            f"'{folder_id}' in parents "
            "and trashed = false "
            "and mimeType != 'application/vnd.google-apps.folder'"
        )

        files: list[dict[str, str | list[str] | None]] = []
        page_token = None

        try:
            while True:
                response = (
                    client.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields=(
                            "nextPageToken, files("
                            "id, name, mimeType, createdTime, modifiedTime, parents)"
                        ),
                        pageToken=page_token,
                    )
                    .execute()
                )

                for file in response.get("files", []):
                    if file.get("name", "").lower().endswith(".xls"):
                        files.append(
                            {
                                "id": file.get("id"),
                                "name": file.get("name"),
                                "mimeType": file.get("mimeType"),
                                "createdTime": file.get("createdTime"),
                                "modifiedTime": file.get("modifiedTime"),
                                "parents": file.get("parents", []),
                            }
                        )

                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as exc:
            status_code = getattr(exc.resp, "status", None)

            if status_code in {401, 403}:
                raise DriveServiceError(
                    "Error de autenticacion o permisos al acceder a Google Drive."
                ) from exc
            if status_code == 404:
                raise DriveServiceError(
                    "La carpeta configurada no existe o no es accesible para la service account."
                ) from exc

            raise DriveServiceError(
                "Ocurrio un error al listar archivos en Google Drive."
            ) from exc

        return files

    def preview_xls_file(
        self, file_id: str, max_rows: int = 5
    ) -> dict[str, str | int | list[list[str | float | bool | None]]]:
        if not file_id:
            raise DriveServiceError("Se requiere un file_id para generar la vista previa.")

        if max_rows < 1:
            raise DriveServiceError("El parametro max_rows debe ser mayor o igual a 1.")

        client = self._get_client()
        buffer = BytesIO()

        try:
            metadata = self.get_file_metadata(file_id)
            request = client.files().get_media(fileId=file_id)
            downloader = MediaIoBaseDownload(buffer, request)

            done = False
            while not done:
                _, done = downloader.next_chunk()
        except HttpError as exc:
            status_code = getattr(exc.resp, "status", None)

            if status_code in {401, 403}:
                raise DriveServiceError(
                    "Error de autenticacion o permisos al acceder al archivo en Google Drive."
                ) from exc
            if status_code == 404:
                raise DriveServiceError(
                    "El archivo solicitado no existe o no es accesible para la service account."
                ) from exc

            raise DriveServiceError(
                "Ocurrio un error al leer el archivo desde Google Drive."
            ) from exc

        try:
            workbook = xlrd.open_workbook(file_contents=buffer.getvalue())
            sheet = workbook.sheet_by_index(0)
        except xlrd.XLRDError as exc:
            raise DriveServiceError(
                "No se pudo interpretar el archivo como un Excel .xls valido."
            ) from exc

        preview_rows: list[list[str | float | bool | None]] = []
        rows_to_read = min(sheet.nrows, max_rows)

        for row_index in range(rows_to_read):
            row_values = sheet.row_values(row_index)
            preview_rows.append([self._normalize_cell_value(value) for value in row_values])

        return {
            "id": metadata.get("id", ""),
            "name": metadata.get("name", ""),
            "mimeType": metadata.get("mimeType", ""),
            "sheetName": sheet.name,
            "totalRows": sheet.nrows,
            "previewRows": preview_rows,
        }

    def get_file_metadata(self, file_id: str) -> dict[str, str | list[str] | None]:
        if not file_id:
            raise DriveServiceError("Se requiere un file_id para consultar metadata.")

        client = self._get_client()

        try:
            metadata = (
                client.files()
                .get(
                    fileId=file_id,
                    fields="id, name, mimeType, createdTime, modifiedTime, parents",
                )
                .execute()
            )
        except HttpError as exc:
            self._raise_http_error(
                exc,
                not_found_message="El archivo solicitado no existe o no es accesible para la service account.",
                forbidden_message="Error de autenticacion o permisos al acceder al archivo en Google Drive.",
                default_message="Ocurrio un error al consultar metadata del archivo en Google Drive.",
            )

        return {
            "id": metadata.get("id"),
            "name": metadata.get("name"),
            "mimeType": metadata.get("mimeType"),
            "createdTime": metadata.get("createdTime"),
            "modifiedTime": metadata.get("modifiedTime"),
            "parents": metadata.get("parents", []),
        }

    def download_file(self, file_id: str) -> dict[str, str]:
        if not file_id:
            raise DriveServiceError("Se requiere un file_id para descargar el archivo.")

        metadata = self.get_file_metadata(file_id)
        file_name = metadata.get("name")

        if not file_name:
            raise DriveServiceError("No se pudo determinar el nombre del archivo a descargar.")

        download_dir = Path(self.settings.local_download_dir)
        try:
            download_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DriveServiceError(
                "No se pudo crear la carpeta temporal configurada para descargas."
            ) from exc

        local_path = download_dir / str(file_name)
        client = self._get_client()
        request = client.files().get_media(fileId=file_id)

        try:
            with local_path.open("wb") as local_file:
                downloader = MediaIoBaseDownload(local_file, request)
                done = False

                # Descargamos en chunks para que luego el metodo sirva tambien para archivos grandes.
                while not done:
                    _, done = downloader.next_chunk()
        except HttpError as exc:
            self._raise_http_error(
                exc,
                not_found_message="El archivo solicitado no existe o no es accesible para la service account.",
                forbidden_message="Error de autenticacion o permisos al descargar el archivo desde Google Drive.",
                default_message="Ocurrio un error al descargar el archivo desde Google Drive.",
            )
        except OSError as exc:
            raise DriveServiceError(
                "No se pudo escribir el archivo descargado en la carpeta temporal local."
            ) from exc

        return {
            "status": "downloaded",
            "file_id": str(metadata.get("id") or file_id),
            "name": str(file_name),
            "local_path": str(local_path),
        }

    def _normalize_cell_value(self, value: object) -> str | float | bool | None:
        if value == "":
            return None
        return value

    def _raise_http_error(
        self,
        exc: HttpError,
        *,
        not_found_message: str,
        forbidden_message: str,
        default_message: str,
    ) -> None:
        status_code = getattr(exc.resp, "status", None)

        if status_code in {401, 403}:
            raise DriveServiceError(forbidden_message) from exc
        if status_code == 404:
            raise DriveServiceError(not_found_message) from exc

        raise DriveServiceError(default_message) from exc

    def _get_client(self):
        if self._client is not None:
            return self._client

        credentials_path = self.settings.google_service_account_file
        if not credentials_path:
            raise DriveServiceError(
                "Falta la variable GOOGLE_SERVICE_ACCOUNT_FILE en la configuracion."
            )

        credentials_file = Path(credentials_path)
        if not credentials_file.is_file():
            raise DriveServiceError(
                "No se encontro el archivo de credenciales indicado en GOOGLE_SERVICE_ACCOUNT_FILE."
            )

        try:
            credentials = Credentials.from_service_account_file(
                str(credentials_file),
                scopes=[self.DRIVE_READONLY_SCOPE],
            )
            # cacheamos el cliente para reutilizarlo y luego extender la clase facilmente
            self._client = build("drive", "v3", credentials=credentials)
        except DefaultCredentialsError as exc:
            raise DriveServiceError(
                "No se pudieron cargar las credenciales de la service account."
            ) from exc
        except ValueError as exc:
            raise DriveServiceError(
                "El archivo de credenciales de Google Drive no es valido."
            ) from exc

        return self._client
