from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base


class MaestroProducto(Base):
    __tablename__ = "maestro_productos"
    __table_args__ = {
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    producto: Mapped[str] = mapped_column(
        String(255, collation="utf8mb4_unicode_ci"),
        nullable=False,
        unique=True,
    )
    id_anunciante: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "base_anunciantes.id_anunciante",
            name="fk_maestro_productos_anunciante",
        ),
        nullable=True,
    )

    anunciante: Mapped["BaseAnunciante | None"] = relationship(
        "BaseAnunciante",
        back_populates="maestro_productos",
    )

    temas: Mapped[list["ProductoTema"]] = relationship(
        "ProductoTema",
        back_populates="producto",
    )
