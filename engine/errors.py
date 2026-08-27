class HarnessError(RuntimeError):
    """Expected workflow or runtime failure."""


class ValidationError(ValueError):
    """Invalid persisted or model-produced data."""


class GitError(RuntimeError):
    """Git inspection failure."""
