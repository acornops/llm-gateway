class SecretNotFoundError(ValueError):
    """Raised when a scoped secret lookup completes successfully but no value exists."""
