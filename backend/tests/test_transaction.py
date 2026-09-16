"""Comprehensive test suite for Phase 3: Transaction Manager."""

import pytest
from app.transaction.exceptions import (
    InvalidTransactionStateError,
    TransactionManagerClosedError,
    TransactionManagerError,
    TransactionNotFoundError,
)
from app.transaction.transaction import (
    Transaction,
    TransactionState,
    TransactionStatus,
)
from app.transaction.transaction_manager import TransactionManager


# =====================================================================
# Unit Tests: Transaction Object & States
# =====================================================================


class TestTransactionUnit:
    """Unit tests for individual Transaction objects and state transitions."""

    def test_transaction_initialization(self):
        """Transaction initializes with given ID and default RUNNING state."""
        tx = Transaction(transaction_id=1)
        assert tx.transaction_id == 1
        assert tx.state == TransactionState.RUNNING
        assert tx.status == TransactionStatus.RUNNING
        assert tx.is_running
        assert not tx.is_committed
        assert not tx.is_aborted

    def test_transaction_invalid_id(self):
        """Non-positive or non-integer IDs must raise ValueError."""
        for bad_id in [0, -1, -99, True, False, "1", 2.5, None]:
            with pytest.raises(ValueError):
                Transaction(transaction_id=bad_id)  # type: ignore

    def test_direct_commit_transition(self):
        """RUNNING -> COMMITTED state transition."""
        tx = Transaction(transaction_id=1)
        tx.commit()
        assert tx.state == TransactionState.COMMITTED
        assert tx.is_committed
        assert not tx.is_running

    def test_direct_abort_transition(self):
        """RUNNING -> ABORTED state transition."""
        tx = Transaction(transaction_id=1)
        tx.abort()
        assert tx.state == TransactionState.ABORTED
        assert tx.is_aborted
        assert not tx.is_running

    def test_invalid_transitions_on_committed_transaction(self):
        """Committed transactions cannot be committed again or aborted."""
        tx = Transaction(transaction_id=1)
        tx.commit()

        with pytest.raises(InvalidTransactionStateError):
            tx.commit()

        with pytest.raises(InvalidTransactionStateError):
            tx.abort()

    def test_invalid_transitions_on_aborted_transaction(self):
        """Aborted transactions cannot be aborted again or committed."""
        tx = Transaction(transaction_id=1)
        tx.abort()

        with pytest.raises(InvalidTransactionStateError):
            tx.abort()

        with pytest.raises(InvalidTransactionStateError):
            tx.commit()

    def test_transaction_repr_and_equality(self):
        """Repr string and equality checks."""
        tx1 = Transaction(transaction_id=1)
        tx2 = Transaction(transaction_id=1)
        tx3 = Transaction(transaction_id=2)

        assert tx1 == tx2
        assert tx1 != tx3
        assert "Transaction(id=1" in repr(tx1)


# =====================================================================
# Integration Tests: TransactionManager
# =====================================================================


class TestTransactionManager:
    """Core tests for TransactionManager lifecycle, table management, and transitions."""

    def test_begin_transaction(self):
        """Requirement A: Begin transaction creates RUNNING transaction in table."""
        tm = TransactionManager()
        tx = tm.begin_transaction()

        assert tx.transaction_id == 1
        assert tx.state == TransactionState.RUNNING
        assert tm.has_transaction(1)
        assert tm.get_transaction(1) == tx
        assert len(tm.get_active_transactions()) == 1

    def test_multiple_transactions_uniqueness_and_monotonicity(self):
        """Requirements B & K: Unique, monotonically increasing IDs in table."""
        tm = TransactionManager()
        txs = [tm.begin_transaction() for _ in range(5)]

        ids = [tx.transaction_id for tx in txs]
        assert ids == [1, 2, 3, 4, 5]
        assert len(set(ids)) == 5

        table = tm.get_transaction_table()
        assert len(table) == 5
        for tx in txs:
            assert tm.has_transaction(tx.transaction_id)
            assert table[tx.transaction_id] == tx

    def test_commit_transaction(self):
        """Requirement C: Commit transitions state to COMMITTED."""
        tm = TransactionManager()
        tx = tm.begin_transaction()
        tx_id = tx.transaction_id

        committed_tx = tm.commit_transaction(tx_id)
        assert committed_tx.state == TransactionState.COMMITTED
        assert committed_tx.is_committed
        assert tm.get_transaction(tx_id).is_committed

        # No longer in active transactions
        assert tm.get_active_transactions() == []
        # Remains in table for inspection
        assert tm.has_transaction(tx_id)

    def test_abort_transaction(self):
        """Requirement D: Abort transitions state to ABORTED."""
        tm = TransactionManager()
        tx = tm.begin_transaction()
        tx_id = tx.transaction_id

        aborted_tx = tm.abort_transaction(tx_id)
        assert aborted_tx.state == TransactionState.ABORTED
        assert aborted_tx.is_aborted
        assert tm.get_transaction(tx_id).is_aborted

        # No longer active
        assert tm.get_active_transactions() == []
        # Remains in table
        assert tm.has_transaction(tx_id)

    def test_valid_state_transitions(self):
        """Requirement E: Verify RUNNING -> COMMITTED and RUNNING -> ABORTED."""
        tm = TransactionManager()

        tx1 = tm.begin_transaction()
        tx2 = tm.begin_transaction()

        tm.commit_transaction(tx1.transaction_id)
        tm.abort_transaction(tx2.transaction_id)

        assert tm.get_transaction(tx1.transaction_id).state == TransactionState.COMMITTED
        assert tm.get_transaction(tx2.transaction_id).state == TransactionState.ABORTED

    def test_invalid_state_transitions_via_manager(self):
        """Requirement F: Re-commit or re-abort completed transactions is rejected."""
        tm = TransactionManager()
        tx1 = tm.begin_transaction()
        tx2 = tm.begin_transaction()

        tm.commit_transaction(tx1.transaction_id)
        tm.abort_transaction(tx2.transaction_id)

        # COMMITTED -> COMMITTED
        with pytest.raises(InvalidTransactionStateError):
            tm.commit_transaction(tx1.transaction_id)

        # COMMITTED -> ABORTED
        with pytest.raises(InvalidTransactionStateError):
            tm.abort_transaction(tx1.transaction_id)

        # ABORTED -> COMMITTED
        with pytest.raises(InvalidTransactionStateError):
            tm.commit_transaction(tx2.transaction_id)

        # ABORTED -> ABORTED
        with pytest.raises(InvalidTransactionStateError):
            tm.abort_transaction(tx2.transaction_id)

    def test_unknown_transaction_operations(self):
        """Requirement G: Operations on nonexistent transaction IDs fail clearly."""
        tm = TransactionManager()
        tm.begin_transaction()

        with pytest.raises(TransactionNotFoundError):
            tm.get_transaction(999)

        with pytest.raises(TransactionNotFoundError):
            tm.commit_transaction(999)

        with pytest.raises(TransactionNotFoundError):
            tm.abort_transaction(999)

        with pytest.raises(TransactionNotFoundError):
            tm.get_transaction(-5)

        with pytest.raises(TransactionNotFoundError):
            tm.get_transaction("1")  # type: ignore

    def test_transaction_lookup_and_existence(self):
        """Requirements H & I: get_transaction and has_transaction correctness."""
        tm = TransactionManager()
        tx = tm.begin_transaction()

        assert tm.has_transaction(tx.transaction_id) is True
        assert tm.has_transaction(42) is False
        assert tm.has_transaction("invalid") is False  # type: ignore

        retrieved = tm.get_transaction(tx.transaction_id)
        assert retrieved.transaction_id == tx.transaction_id
        assert retrieved.state == TransactionState.RUNNING

    def test_transaction_table_inspection_isolation(self):
        """Requirement J: get_transaction_table returns a protected copy."""
        tm = TransactionManager()
        tx1 = tm.begin_transaction()
        tx2 = tm.begin_transaction()

        table = tm.get_transaction_table()
        assert len(table) == 2
        assert table[1] == tx1
        assert table[2] == tx2

        # Mutating the returned dictionary must not affect internal state
        del table[1]
        assert tm.has_transaction(1) is True
        assert len(tm.get_transaction_table()) == 2

    def test_lifecycle_and_close(self):
        """Requirement L: Operations after close() are rejected."""
        tm = TransactionManager()
        tx = tm.begin_transaction()

        tm.close()
        assert tm.is_closed

        with pytest.raises(TransactionManagerClosedError):
            tm.begin_transaction()

        with pytest.raises(TransactionManagerClosedError):
            tm.commit_transaction(tx.transaction_id)

        with pytest.raises(TransactionManagerClosedError):
            tm.abort_transaction(tx.transaction_id)

        with pytest.raises(TransactionManagerClosedError):
            tm.get_transaction(tx.transaction_id)

        with pytest.raises(TransactionManagerClosedError):
            tm.has_transaction(tx.transaction_id)

        with pytest.raises(TransactionManagerClosedError):
            tm.get_active_transactions()

        with pytest.raises(TransactionManagerClosedError):
            tm.get_transaction_table()

    def test_context_manager_support(self):
        """TransactionManager can be used cleanly with a context manager."""
        with TransactionManager() as tm:
            tx = tm.begin_transaction()
            tm.commit_transaction(tx.transaction_id)
            assert not tm.is_closed

        assert tm.is_closed
