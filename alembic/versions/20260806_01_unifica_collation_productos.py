"""Unifica la collation de las tablas normalizadas de productos.

Revision ID: 20260806_01
Revises:
Create Date: 2026-08-06
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260806_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Use the same character set and collation as cronogramas."""
    op.execute(
        "ALTER TABLE maestro_productos "
        "CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    op.execute(
        "ALTER TABLE productos_temas "
        "CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )


def downgrade() -> None:
    """Restore the original MySQL 8 collation."""
    op.execute(
        "ALTER TABLE productos_temas "
        "CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
    )
    op.execute(
        "ALTER TABLE maestro_productos "
        "CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
    )
