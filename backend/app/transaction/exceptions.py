"""Custom exceptions for the transaction management layer."""


class TransactionManagerError(Exception):
    """Base exception for all transaction manager errors."""
    pass


class TransactionNotFoundError(TransactionManagerError):
    """Raised when an operation targets a nonexistent transaction ID."""
    pass


class InvalidTransactionStateError(TransactionManagerError):
    """Raised when an illegal transaction state transition is attempted."""
    pass


class TransactionManagerClosedError(TransactionManagerError):
    """Raised when operations are attempted on a closed TransactionManager."""
    pass
