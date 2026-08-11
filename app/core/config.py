from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuracion central de la aplicacion cargada desde variables de entorno."""

    app_name: str = "Monitor de Archivos"
    database_url: str | None = None
    google_service_account_file: str | None = None
    google_drive_input_folder_id: str | None = None
    local_download_dir: str = "tmp/downloads"
    session_secret_key: str = "change-me-in-production"
    session_cookie_secure: bool = False
    local_admin_username: str = "admin"
    local_admin_password: str = ""
    auth_local_users: str | None = None
    seed_admin_nombre: str = "Administrador local"
    seed_admin_email: str | None = None
    ad_server_ip: str | None = None
    ad_server_port: int = 389
    ad_connect_timeout: int = 5
    ad_use_ssl: bool = False
    ad_tls_validate_cert: bool = False
    ad_bind_user: str | None = None
    ad_bind_password: str | None = None
    ad_base_dn: str | None = None
    ad_groups_base_dn: str | None = None
    ad_domain: str | None = None
    ad_login_attr: str = "sAMAccountName"
    ad_group_name: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
