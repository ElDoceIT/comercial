from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base


class BaseAnunciante(Base):
    __tablename__ = "base_anunciantes"

    id_anunciante: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    anunciante: Mapped[str | None] = mapped_column(String(255), nullable=True)
    id_cliente: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alcance: Mapped[str | None] = mapped_column(String(50), nullable=True)
    categoria: Mapped[str | None] = mapped_column(String(100), nullable=True)

    maestro_productos: Mapped[list["MaestroProducto"]] = relationship(
        "MaestroProducto",
        back_populates="anunciante",
    )
