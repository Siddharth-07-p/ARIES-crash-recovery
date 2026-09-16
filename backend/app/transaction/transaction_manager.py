"""TransactionManager managing in-memory active transaction state and lifecycle."""

from typing import Dict, List, Optional
from app.transaction.exceptions import (
    InvalidTransactionStateError,
    TransactionManagerClosedError,
    TransactionNotFoundError,
)
from app.transaction.transaction import Transaction, TransactionState


class TransactionManager:
    """Manages active transaction lifecycles, states, and the in-memory transaction table."""

    def __init__(self) -> None:
        """Initialize TransactionManager with an empty transaction table."""
        self._next_tx_id: int = 1
        self._transactions: Dict[int, Transaction] = {}
        self._is_closed: bool = False

    @property
    def is_closed(self) -> bool:
        """Check whether the TransactionManager is closed."""
        return self._is_closed

    def _check_open(self) -> None:
        """Verify that TransactionManager is open.

        Raises:
            TransactionManagerClosedError: If the manager has been closed.
        """
        if self._is_closed:
            raise TransactionManagerClosedError(
                "TransactionManager is closed. No operations permitted."
            )

    def begin_transaction(self) -> Transaction:
        """Begin a new database transaction in RUNNING state.

        Returns:
            The newly created Transaction instance.

        Raises:
            TransactionManagerClosedError: If the manager is closed.
        """
        self._check_open()

        tx_id = self._next_tx_id
        self._next_tx_id += 1

        tx = Transaction(transaction_id=tx_id, state=TransactionState.RUNNING)
        self._transactions[tx_id] = tx
        return tx

    def commit_transaction(self, transaction_id: int) -> Transaction:
        """Commit an active transaction, transitioning its state to COMMITTED.

        Args:
            transaction_id: Unique integer identifier of the transaction.

        Returns:
            The committed Transaction instance.

        Raises:
            TransactionManagerClosedError: If the manager is closed.
            TransactionNotFoundError: If the transaction does not exist.
            InvalidTransactionStateError: If the transaction is not in RUNNING state.
        """
        self._check_open()

        if not isinstance(transaction_id, int) or isinstance(transaction_id, bool):
            raise TransactionNotFoundError(
                f"Invalid transaction ID: {transaction_id!r}"
            )

        if transaction_id not in self._transactions:
            raise TransactionNotFoundError(
                f"Transaction {transaction_id} not found."
            )

        tx = self._transactions[transaction_id]
        tx.commit()
        return tx

    def abort_transaction(self, transaction_id: int) -> Transaction:
        """Abort an active transaction, transitioning its state to ABORTED.

        Note: Physical rollback is not performed in Phase 3.

        Args:
            transaction_id: Unique integer identifier of the transaction.

        Returns:
            The aborted Transaction instance.

        Raises:
            TransactionManagerClosedError: If the manager is closed.
            TransactionNotFoundError: If the transaction does not exist.
            InvalidTransactionStateError: If the transaction is not in RUNNING state.
        """
        self._check_open()

        if not isinstance(transaction_id, int) or isinstance(transaction_id, bool):
            raise TransactionNotFoundError(
                f"Invalid transaction ID: {transaction_id!r}"
            )

        if transaction_id not in self._transactions:
            raise TransactionNotFoundError(
                f"Transaction {transaction_id} not found."
            )

        tx = self._transactions[transaction_id]
        tx.abort()
        return tx

    def get_transaction(self, transaction_id: int) -> Transaction:
        """Retrieve a transaction by ID from the transaction table.

        Args:
            transaction_id: Target transaction ID.

        Returns:
            The Transaction instance.

        Raises:
            TransactionManagerClosedError: If the manager is closed.
            TransactionNotFoundError: If the transaction does not exist.
        """
        self._check_open()

        if not isinstance(transaction_id, int) or isinstance(transaction_id, bool):
            raise TransactionNotFoundError(
                f"Invalid transaction ID: {transaction_id!r}"
            )

        if transaction_id not in self._transactions:
            raise TransactionNotFoundError(
                f"Transaction {transaction_id} not found."
            )

        return self._transactions[transaction_id]

    def has_transaction(self, transaction_id: int) -> bool:
        """Check whether a transaction exists in the transaction table.

        Args:
            transaction_id: Target transaction ID.

        Returns:
            True if transaction exists, False otherwise.

        Raises:
            TransactionManagerClosedError: If the manager is closed.
        """
        self._check_open()

        if not isinstance(transaction_id, int) or isinstance(transaction_id, bool):
            return False

        return transaction_id in self._transactions

    def get_active_transactions(self) -> List[Transaction]:
        """Return a list of all transactions currently in RUNNING state.

        Returns:
            List of active Transaction objects.

        Raises:
            TransactionManagerClosedError: If the manager is closed.
        """
        self._check_open()
        return [tx for tx in self._transactions.values() if tx.is_running]

    def get_transaction_table(self) -> Dict[int, Transaction]:
        """Return a snapshot copy of the full transaction table.

        Returns:
            Dictionary mapping transaction_id to Transaction instance.

        Raises:
            TransactionManagerClosedError: If the manager is closed.
        """
        self._check_open()
        return dict(self._transactions)

    def close(self) -> None:
        """Close the TransactionManager and prevent further operations."""
        self._is_closed = True

    def __enter__(self) -> "TransactionManager":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()

    def __repr__(self) -> str:
        """String representation of TransactionManager."""
        status = "closed" if self.is_closed else "open"
        return (
            f"TransactionManager(total_transactions={len(self._transactions)}, "
            f"active_transactions={len(self.get_active_transactions() if not self.is_closed else [])}, "
            f"status='{status}')"
        )
