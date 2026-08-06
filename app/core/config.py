from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuracion central de la aplicacion cargada desde variables de entorno."""

    app_name: str = "Monitor de Archivos"
    database_url: str | None = None
    google_service_account_file: str | None = None
    google_drive_input_folder_id: str | None = None
    local_download_dir: str = "tmp/downloads"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
