from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base


class ProductoTema(Base):
    __tablename__ = "productos_temas"
    __table_args__ = (
        Index("idx_productos_temas_id_producto", "id_producto"),
        Index("idx_productos_temas_cod_producto", "cod_producto"),
        {
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    id_producto: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "maestro_productos.id",
            name="fk_productos_temas_producto",
        ),
        nullable=False,
    )
    cod_producto: Mapped[str | None] = mapped_column(
        String(50, collation="utf8mb4_unicode_ci"),
        nullable=True,
    )
    tema: Mapped[str | None] = mapped_column(
        String(255, collation="utf8mb4_unicode_ci"),
        nullable=True,
    )

    producto: Mapped["MaestroProducto"] = relationship(
        "MaestroProducto",
        back_populates="temas",
    )
