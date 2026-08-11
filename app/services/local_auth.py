import logging
from typing import Protocol

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.usuarios import Usuario


logger = logging.getLogger(__name__)


class SessionUserData(Protocol):
    id: int
    username: str
    display_name: str
    email: str | None
    perfil: str | None
    rol: str | None


def normalize_username(username: str) -> str:
    return username.strip().lower()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def find_user(db: Session, username: str) -> Usuario | None:
    normalized = normalize_username(username)
    if not normalized:
        return None
    return db.scalar(
        select(Usuario).where(func.lower(Usuario.username) == normalized)
    )


def authenticate_local_user(
    db: Session,
    username: str,
    password: str,
) -> Usuario | None:
    user = find_user(db, username)
    if (
        user is None
        or not user.status
        or user.origen.strip().upper() != "LOCAL"
        or not password
        or not user.hashed_password
    ):
        return None
    try:
        valid = bcrypt.checkpw(
            password.encode("utf-8"),
            user.hashed_password.encode("utf-8"),
        )
    except (TypeError, ValueError):
        logger.warning("El hash del usuario local %s no es válido.", user.username)
        return None
    return user if valid else None


def session_user(user: SessionUserData, source: str) -> dict[str, object]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "source": source,
        "role": user.rol,
        "profile": user.perfil,
    }
