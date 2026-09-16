"""Transaction abstraction and lifecycle state enumeration."""

from enum import Enum
from typing import Any
from app.transaction.exceptions import InvalidTransactionStateError


class TransactionState(str, Enum):
    """Lifecycle states for database transactions."""

    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"


# Alias for compatibility with both state and status naming
TransactionStatus = TransactionState


class Transaction:
    """In-memory representation of an individual database transaction.

    Attributes:
        transaction_id: Unique positive integer identifying the transaction.
        state: Current lifecycle state (RUNNING, COMMITTED, ABORTED).
    """

    __slots__ = ("_transaction_id", "_state")

    def __init__(
        self,
        transaction_id: int,
        state: TransactionState = TransactionState.RUNNING,
    ) -> None:
        """Initialize a Transaction object.

        Args:
            transaction_id: Unique positive integer identifier.
            state: Initial state (default RUNNING).

        Raises:
            ValueError: If transaction_id is not a positive integer.
        """
        if not isinstance(transaction_id, int) or isinstance(transaction_id, bool) or transaction_id <= 0:
            raise ValueError(
                f"transaction_id must be a positive integer, got {transaction_id!r}"
            )

        self._transaction_id: int = transaction_id
        self._state: TransactionState = state

    @property
    def transaction_id(self) -> int:
        """Return the transaction ID."""
        return self._transaction_id

    @property
    def state(self) -> TransactionState:
        """Return the current transaction state."""
        return self._state

    @property
    def status(self) -> TransactionStatus:
        """Alias property returning the current transaction status."""
        return self._state

    @property
    def is_running(self) -> bool:
        """Return True if transaction is currently RUNNING."""
        return self._state == TransactionState.RUNNING

    @property
    def is_committed(self) -> bool:
        """Return True if transaction has been COMMITTED."""
        return self._state == TransactionState.COMMITTED

    @property
    def is_aborted(self) -> bool:
        """Return True if transaction has been ABORTED."""
        return self._state == TransactionState.ABORTED

    def commit(self) -> None:
        """Transition transaction state from RUNNING to COMMITTED.

        Raises:
            InvalidTransactionStateError: If current state is not RUNNING.
        """
        if self._state != TransactionState.RUNNING:
            raise InvalidTransactionStateError(
                f"Cannot commit transaction {self._transaction_id}: "
                f"current state is {self._state.value}, expected RUNNING."
            )
        self._state = TransactionState.COMMITTED

    def abort(self) -> None:
        """Transition transaction state from RUNNING to ABORTED.

        Raises:
            InvalidTransactionStateError: If current state is not RUNNING.
        """
        if self._state != TransactionState.RUNNING:
            raise InvalidTransactionStateError(
                f"Cannot abort transaction {self._transaction_id}: "
                f"current state is {self._state.value}, expected RUNNING."
            )
        self._state = TransactionState.ABORTED

    def __eq__(self, other: Any) -> bool:
        """Check equality with another Transaction."""
        if not isinstance(other, Transaction):
            return False
        return (
            self._transaction_id == other._transaction_id
            and self._state == other._state
        )

    def __repr__(self) -> str:
        """String representation of the transaction."""
        return f"Transaction(id={self._transaction_id}, state={self._state.value})"
