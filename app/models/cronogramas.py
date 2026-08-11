from datetime import date

from sqlalchemy import Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base


class Cronograma(Base):
    __tablename__ = "cronogramas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    archivo_ingesta_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hora_inicio: Mapped[str | None] = mapped_column(String(20), nullable=True)
    hora_fin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cod_prod: Mapped[str | None] = mapped_column(String(50), nullable=True)
    producto: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tema: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duracion: Mapped[int | None] = mapped_column(Integer, nullable=True)
    t_compra: Mapped[str | None] = mapped_column(String(50), nullable=True)
    t_material: Mapped[str | None] = mapped_column(String(50), nullable=True)
    columna_extra: Mapped[str | None] = mapped_column(String(255), nullable=True)
    alcance: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prom_canal: Mapped[str | None] = mapped_column(String(100), nullable=True)
    programa: Mapped[str | None] = mapped_column(String(255), nullable=True)
    canal: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fecha: Mapped[date | None] = mapped_column(Date, nullable=True)
    dia_semana: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tipo_dia: Mapped[str | None] = mapped_column(String(50), nullable=True)
