from __future__ import annotations


class ServiceError(Exception):
    """Base application service error."""


class NotFoundError(ServiceError, ValueError):
    """Raised when a requested entity does not exist."""


class ValidationError(ServiceError, ValueError):
    """Raised when a request payload is invalid."""


class ConflictError(ServiceError, PermissionError):
    """Raised when an action is not allowed in the current state."""
