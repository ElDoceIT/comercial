from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base


class ArchivoIngesta(Base):
    __tablename__ = "archivos_ingesta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_file_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    nombre_archivo: Mapped[str] = mapped_column(String(255), nullable=False)
    canal_codigo: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fecha_nombre_archivo: Mapped[date | None] = mapped_column(Date, nullable=True)
    cantidad_dias_detectados: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fechas_detectadas: Mapped[str | None] = mapped_column(Text, nullable=True)
    estado: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pendiente", server_default="pendiente"
    )
    procesamiento_completo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    fecha_inicio_proceso: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fecha_fin_proceso: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    filas_procesadas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_detalle: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        onupdate=func.current_timestamp(),
    )
