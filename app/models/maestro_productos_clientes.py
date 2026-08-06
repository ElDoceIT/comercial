from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base


class MaestroProductosClientes(Base):
    __tablename__ = "maestro_productos_clientes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cod_prod: Mapped[str | None] = mapped_column(String(50), nullable=True)
    producto: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tema: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cliente: Mapped[str | None] = mapped_column(String(255), nullable=True)
    alcance: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prom_canal: Mapped[str | None] = mapped_column(String(100), nullable=True)
    activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    observaciones: Mapped[str | None] = mapped_column(Text, nullable=True)
