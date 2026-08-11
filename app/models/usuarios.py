from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    correo: Mapped[str | None] = mapped_column(String(150), nullable=True)
    apellido: Mapped[str] = mapped_column(String(100), nullable=False)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    origen: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[bool] = mapped_column(Boolean, nullable=False)
    perfil: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rol: Mapped[str | None] = mapped_column(String(50), nullable=True)

    @property
    def display_name(self) -> str:
        return f"{self.nombre} {self.apellido}".strip() or self.username

    @property
    def email(self) -> str | None:
        return self.correo
