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


class DocumentNotFoundError(DocuMindException):
    """Raised when a referenced document does not exist."""

    status_code = 404


class PersistenceError(DocuMindException):
    """Raised when the database rejects a write; the underlying error is chained, not exposed."""

    status_code = 500

    def __init__(self, message: str, code: str = "PERSISTENCE_ERROR") -> None:
        super().__init__(message, code)


class InvalidUploadError(DocuMindException):
    """Raised when an uploaded file fails extension, MIME, or content validation."""

    status_code = 400

    def __init__(self, message: str, code: str = "INVALID_UPLOAD") -> None:
        super().__init__(message, code)


class FileTooLargeError(InvalidUploadError):
    """Raised when an uploaded file exceeds the configured size limit."""

    status_code = 413

    def __init__(self, message: str) -> None:
        super().__init__(message, "FILE_TOO_LARGE")


class ChunkingError(DocuMindException):
    """Raised when chunking cannot run: invalid size settings or an unavailable tokenizer."""

    status_code = 500

    def __init__(self, message: str, code: str = "CHUNKING_ERROR") -> None:
        super().__init__(message, code)


class EmbeddingError(DocuMindException):
    """Raised when embeddings cannot be generated: invalid input, provider error or bad response.

    The provider's own error is chained, never copied into the message.
    """

    status_code = 502

    def __init__(self, message: str, code: str = "EMBEDDING_ERROR") -> None:
        super().__init__(message, code)


class RetrievalError(DocuMindException):
    """Raised when a vector search is given invalid input (project, query, top_k, embedding)."""

    status_code = 400

    def __init__(self, message: str, code: str = "RETRIEVAL_ERROR") -> None:
        super().__init__(message, code)


class EmbeddingRateLimitError(EmbeddingError):
    """Raised when the embedding provider keeps rate limiting after all retries."""

    status_code = 503

    def __init__(self, message: str, code: str = "EMBEDDING_RATE_LIMITED") -> None:
        super().__init__(message, code)
