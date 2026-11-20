"""Public imports for the nimstatus client distribution."""

from nimstatus_client import (
    DEFAULT_STATUS_URL,
    ModelRouter,
    NoModelsAvailable,
    __version__,
    call_with_fallback,
    is_retryable,
    resolve,
)

__all__ = [
    "DEFAULT_STATUS_URL",
    "ModelRouter",
    "NoModelsAvailable",
    "call_with_fallback",
    "is_retryable",
    "resolve",
]
