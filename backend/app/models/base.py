"""SQLAlchemy 2.0 declarative base shared by every ORM model.

Models inherit from :class:`Base` and use ``Mapped[...]`` / ``mapped_column(...)``.
``Base.metadata`` is the single registry Alembic will target for autogeneration.
"""

import uuid

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names keep Alembic migrations stable and reversible.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def generate_uuid() -> str:
    """Return a new random UUID as its 36-character string form (the primary-key format)."""
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base class for all DocuMind ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
