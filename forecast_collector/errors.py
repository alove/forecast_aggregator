class CollectorError(RuntimeError):
    """Base exception for expected collection or validation failures."""


class FetchError(CollectorError):
    """Raised when a public source cannot be retrieved after retries."""


class SourceFormatError(CollectorError):
    """Raised when a provider changes or publishes an incomplete schema."""


class OutputValidationError(CollectorError):
    """Raised when normalized output violates the collector schema."""
