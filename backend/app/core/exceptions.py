"""Domain exception hierarchy (see docs/RULES.md section 2.3)."""


class DocuMindException(Exception):
    """Base exception for all domain errors."""

    status_code = 500

    def __init__(self, message: str, code: str = "INTERNAL_ERROR") -> None:
        self.message = message
        self.code = code
        super().__init__(self.message)


class DocumentParsingError(DocuMindException):
    """Raised when an uploaded document cannot be parsed."""

    status_code = 422


class EndpointNotFoundError(DocuMindException):
    """Raised when a requested API endpoint is absent from the catalog."""

    status_code = 404
