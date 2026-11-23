"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.base import Base
from app.models.document import Document, DocumentFileType, DocumentStatus
from app.models.endpoint import Endpoint
from app.models.project import Project

__all__ = [
    "Base",
    "Document",
    "DocumentFileType",
    "DocumentStatus",
    "Endpoint",
    "Project",
]
