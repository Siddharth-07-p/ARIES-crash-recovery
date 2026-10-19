"""Data structures representing reconstructed recovery state from ARIES Analysis."""

from typing import Any, Dict, List, Optional, Union
from app.checkpoint.checkpoint import CheckpointInfo
from app.recovery.exceptions import AnalysisError, RedoError, UndoError
from app.transaction.transaction import TransactionState



class RecoveredTransaction:
    """Represents a transaction reconstructed during crash recovery Analysis.

    Attributes:
        transaction_id: Unique positive integer identifying the transaction.
        state: Reconstructed TransactionState (RUNNING, COMMITTED, ABORTED).
        last_lsn: Most recent LSN written by this transaction.
    """

    __slots__ = ("_transaction_id", "_state", "_last_lsn")

    def __init__(
        self,
        transaction_id: int,
        state: Union[TransactionState, str] = TransactionState.RUNNING,
        last_lsn: Optional[int] = None,
    ) -> None:
        """Initialize RecoveredTransaction."""
        if not isinstance(transaction_id, int) or isinstance(transaction_id, bool) or transaction_id <= 0:
            raise AnalysisError(f"transaction_id must be a positive integer, got {transaction_id!r}")

        if isinstance(state, str):
            try:
                state = TransactionState(state)
            except ValueError:
                raise AnalysisError(f"Invalid transaction state: {state!r}")

        if not isinstance(state, TransactionState):
            raise AnalysisError(f"state must be TransactionState, got {type(state).__name__}")

        if last_lsn is not None:
            if not isinstance(last_lsn, int) or isinstance(last_lsn, bool) or last_lsn <= 0:
                raise AnalysisError(f"last_lsn must be a positive integer or None, got {last_lsn!r}")

        self._transaction_id: int = transaction_id
        self._state: TransactionState = state
        self._last_lsn: Optional[int] = last_lsn

    @property
    def transaction_id(self) -> int:
        """Return the transaction ID."""
        return self._transaction_id

    @property
    def state(self) -> TransactionState:
        """Return the transaction state."""
        return self._state

    @state.setter
    def state(self, new_state: Union[TransactionState, str]) -> None:
        """Update transaction state."""
        if isinstance(new_state, str):
            try:
                new_state = TransactionState(new_state)
            except ValueError:
                raise AnalysisError(f"Invalid transaction state: {new_state!r}")
        if not isinstance(new_state, TransactionState):
            raise AnalysisError(f"state must be TransactionState, got {type(new_state).__name__}")
        self._state = new_state

    @property
    def last_lsn(self) -> Optional[int]:
        """Return the last LSN for this transaction."""
        return self._last_lsn

    @last_lsn.setter
    def last_lsn(self, lsn: int) -> None:
        """Update the last LSN for this transaction."""
        if not isinstance(lsn, int) or isinstance(lsn, bool) or lsn <= 0:
            raise AnalysisError(f"last_lsn must be a positive integer, got {lsn!r}")
        self._last_lsn = lsn

    def to_dict(self) -> Dict[str, Any]:
        """Convert entry to dictionary representation."""
        return {
            "transaction_id": self._transaction_id,
            "state": self._state.value,
            "last_lsn": self._last_lsn,
        }

    def __eq__(self, other: object) -> bool:
        """Equality check."""
        if not isinstance(other, RecoveredTransaction):
            return False
        return (
            self._transaction_id == other._transaction_id
            and self._state == other._state
            and self._last_lsn == other._last_lsn
        )

    def __repr__(self) -> str:
        """String representation."""
        return f"RecoveredTransaction(id={self._transaction_id}, state={self._state.value}, last_lsn={self._last_lsn})"


class AnalysisResult:
    """Encapsulates the complete result of the ARIES Analysis recovery phase.

    Attributes:
        transaction_table: Reconstructed transaction state (tx_id -> RecoveredTransaction).
        dirty_page_table: Reconstructed Dirty Page Table (page_id -> recLSN).
        winners: List of committed transaction IDs.
        losers: List of uncommitted active (RUNNING) transaction IDs at crash time.
        redo_start_lsn: Smallest recLSN across all dirty pages, or None if DPT is empty.
        analysis_start_lsn: LSN where analysis log scanning commenced.
        last_analyzed_lsn: Highest LSN observed and processed during analysis.
        checkpoint_info: Reference to the checkpoint used as analysis starting point (if any).
    """

    __slots__ = (
        "_transaction_table",
        "_dirty_page_table",
        "_winners",
        "_losers",
        "_redo_start_lsn",
        "_analysis_start_lsn",
        "_last_analyzed_lsn",
        "_checkpoint_info",
    )

    def __init__(
        self,
        transaction_table: Dict[int, RecoveredTransaction],
        dirty_page_table: Dict[int, int],
        winners: List[int],
        losers: List[int],
        redo_start_lsn: Optional[int],
        analysis_start_lsn: Optional[int],
        last_analyzed_lsn: Optional[int],
        checkpoint_info: Optional[CheckpointInfo] = None,
    ) -> None:
        """Initialize immutable AnalysisResult."""
        self._transaction_table: Dict[int, RecoveredTransaction] = {
            k: RecoveredTransaction(v.transaction_id, v.state, v.last_lsn)
            for k, v in transaction_table.items()
        }
        self._dirty_page_table: Dict[int, int] = dict(dirty_page_table)
        self._winners: List[int] = sorted(list(winners))
        self._losers: List[int] = sorted(list(losers))
        self._redo_start_lsn: Optional[int] = redo_start_lsn
        self._analysis_start_lsn: Optional[int] = analysis_start_lsn
        self._last_analyzed_lsn: Optional[int] = last_analyzed_lsn
        self._checkpoint_info: Optional[CheckpointInfo] = checkpoint_info

    @property
    def transaction_table(self) -> Dict[int, RecoveredTransaction]:
        """Return a copy of the reconstructed Transaction Table."""
        return {
            k: RecoveredTransaction(v.transaction_id, v.state, v.last_lsn)
            for k, v in self._transaction_table.items()
        }

    @property
    def dirty_page_table(self) -> Dict[int, int]:
        """Return a copy of the reconstructed Dirty Page Table."""
        return dict(self._dirty_page_table)

    @property
    def winners(self) -> List[int]:
        """Return list of winner (COMMITTED) transaction IDs."""
        return list(self._winners)

    @property
    def losers(self) -> List[int]:
        """Return list of loser (RUNNING at crash) transaction IDs."""
        return list(self._losers)

    @property
    def redo_start_lsn(self) -> Optional[int]:
        """Return the minimum recLSN from DPT, or None if DPT is empty."""
        return self._redo_start_lsn

    @property
    def analysis_start_lsn(self) -> Optional[int]:
        """Return the LSN where analysis log scanning commenced."""
        return self._analysis_start_lsn

    @property
    def last_analyzed_lsn(self) -> Optional[int]:
        """Return the highest LSN analyzed in the WAL."""
        return self._last_analyzed_lsn

    @property
    def checkpoint_info(self) -> Optional[CheckpointInfo]:
        """Return the CheckpointInfo used as the baseline, if any."""
        return self._checkpoint_info

    def to_dict(self) -> Dict[str, Any]:
        """Convert analysis result to dictionary format."""
        return {
            "transaction_table": {
                tx_id: tx.to_dict() for tx_id, tx in sorted(self._transaction_table.items())
            },
            "dirty_page_table": {
                page_id: rec_lsn for page_id, rec_lsn in sorted(self._dirty_page_table.items())
            },
            "winners": self._winners,
            "losers": self._losers,
            "redo_start_lsn": self._redo_start_lsn,
            "analysis_start_lsn": self._analysis_start_lsn,
            "last_analyzed_lsn": self._last_analyzed_lsn,
            "has_checkpoint": self._checkpoint_info is not None,
        }

    def __eq__(self, other: object) -> bool:
        """Equality check."""
        if not isinstance(other, AnalysisResult):
            return False
        return (
            self._transaction_table == other._transaction_table
            and self._dirty_page_table == other._dirty_page_table
            and self._winners == other._winners
            and self._losers == other._losers
            and self._redo_start_lsn == other._redo_start_lsn
            and self._analysis_start_lsn == other._analysis_start_lsn
            and self._last_analyzed_lsn == other._last_analyzed_lsn
        )

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"AnalysisResult(winners={self._winners}, losers={self._losers}, "
            f"dpt_entries={len(self._dirty_page_table)}, redo_start_lsn={self._redo_start_lsn})"
        )


class RedoResult:
    """Encapsulates the complete result of the ARIES Redo recovery phase.

    Attributes:
        redo_start_lsn: LSN where Redo log scanning commenced (or None if DPT was empty).
        last_scanned_lsn: Highest LSN observed and processed during Redo (or None).
        records_scanned: Total number of log records scanned from redo_start_lsn.
        records_redone: Number of UPDATE records successfully replayed to buffer pages.
        records_skipped: Number of records skipped (non-UPDATE, DPT filter, recLSN filter, or page_lsn check).
        pages_modified: Sorted list of unique page IDs modified during Redo.
        pages_skipped: Sorted list of unique page IDs referenced in skipped UPDATE records.
    """

    __slots__ = (
        "_redo_start_lsn",
        "_last_scanned_lsn",
        "_records_scanned",
        "_records_redone",
        "_records_skipped",
        "_pages_modified",
        "_pages_skipped",
    )

    def __init__(
        self,
        redo_start_lsn: Optional[int] = None,
        last_scanned_lsn: Optional[int] = None,
        records_scanned: int = 0,
        records_redone: int = 0,
        records_skipped: int = 0,
        pages_modified: Optional[List[int]] = None,
        pages_skipped: Optional[List[int]] = None,
    ) -> None:
        """Initialize immutable RedoResult."""
        if redo_start_lsn is not None and (not isinstance(redo_start_lsn, int) or isinstance(redo_start_lsn, bool) or redo_start_lsn <= 0):
            raise RedoError(f"redo_start_lsn must be a positive integer or None, got {redo_start_lsn!r}")
        if last_scanned_lsn is not None and (not isinstance(last_scanned_lsn, int) or isinstance(last_scanned_lsn, bool) or last_scanned_lsn <= 0):
            raise RedoError(f"last_scanned_lsn must be a positive integer or None, got {last_scanned_lsn!r}")
        if not isinstance(records_scanned, int) or isinstance(records_scanned, bool) or records_scanned < 0:
            raise RedoError(f"records_scanned must be a non-negative integer, got {records_scanned!r}")
        if not isinstance(records_redone, int) or isinstance(records_redone, bool) or records_redone < 0:
            raise RedoError(f"records_redone must be a non-negative integer, got {records_redone!r}")
        if not isinstance(records_skipped, int) or isinstance(records_skipped, bool) or records_skipped < 0:
            raise RedoError(f"records_skipped must be a non-negative integer, got {records_skipped!r}")

        self._redo_start_lsn: Optional[int] = redo_start_lsn
        self._last_scanned_lsn: Optional[int] = last_scanned_lsn
        self._records_scanned: int = records_scanned
        self._records_redone: int = records_redone
        self._records_skipped: int = records_skipped
        self._pages_modified: List[int] = sorted(list(set(pages_modified))) if pages_modified is not None else []
        self._pages_skipped: List[int] = sorted(list(set(pages_skipped))) if pages_skipped is not None else []

    @property
    def redo_start_lsn(self) -> Optional[int]:
        """Return the Redo starting LSN."""
        return self._redo_start_lsn

    @property
    def last_scanned_lsn(self) -> Optional[int]:
        """Return the highest LSN scanned during Redo."""
        return self._last_scanned_lsn

    @property
    def records_scanned(self) -> int:
        """Return the total number of log records scanned."""
        return self._records_scanned

    @property
    def records_redone(self) -> int:
        """Return the number of UPDATE records replayed."""
        return self._records_redone

    @property
    def records_skipped(self) -> int:
        """Return the number of records skipped."""
        return self._records_skipped

    @property
    def pages_modified(self) -> List[int]:
        """Return a copy of the sorted list of unique modified page IDs."""
        return list(self._pages_modified)

    @property
    def pages_skipped(self) -> List[int]:
        """Return a copy of the sorted list of unique skipped page IDs."""
        return list(self._pages_skipped)

    def to_dict(self) -> Dict[str, Any]:
        """Convert redo result to dictionary format."""
        return {
            "redo_start_lsn": self._redo_start_lsn,
            "last_scanned_lsn": self._last_scanned_lsn,
            "records_scanned": self._records_scanned,
            "records_redone": self._records_redone,
            "records_skipped": self._records_skipped,
            "pages_modified": list(self._pages_modified),
            "pages_skipped": list(self._pages_skipped),
        }

    def __eq__(self, other: object) -> bool:
        """Equality check."""
        if not isinstance(other, RedoResult):
            return False
        return (
            self._redo_start_lsn == other._redo_start_lsn
            and self._last_scanned_lsn == other._last_scanned_lsn
            and self._records_scanned == other._records_scanned
            and self._records_redone == other._records_redone
            and self._records_skipped == other._records_skipped
            and self._pages_modified == other._pages_modified
            and self._pages_skipped == other._pages_skipped
        )

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"RedoResult(redo_start_lsn={self._redo_start_lsn}, "
            f"scanned={self._records_scanned}, redone={self._records_redone}, "
            f"skipped={self._records_skipped}, pages_modified={self._pages_modified})"
        )


class UndoResult:
    """Encapsulates the complete result of the ARIES Undo recovery phase.

    Attributes:
        loser_transactions: Total number of loser transactions identified.
        transactions_undone: Number of transactions successfully rolled back.
        clrs_generated: Number of Compensation Log Records (CLRs) written.
        updates_undone: Number of UPDATE log records undone.
        pages_modified: Sorted list of unique page IDs modified during Undo.
        records_skipped: Number of records skipped (CLRs or non-undo records).
        last_processed_lsn: Most recent/highest LSN processed during Undo (or None).
        completed_transactions: Sorted list of transaction IDs that completed rollback.
        remaining_losers: Sorted list of loser transaction IDs remaining (empty if complete).
        is_complete: True if all loser transactions were successfully undone.
    """

    __slots__ = (
        "_loser_transactions",
        "_transactions_undone",
        "_clrs_generated",
        "_updates_undone",
        "_pages_modified",
        "_records_skipped",
        "_last_processed_lsn",
        "_completed_transactions",
        "_remaining_losers",
        "_is_complete",
    )

    def __init__(
        self,
        loser_transactions: int = 0,
        transactions_undone: int = 0,
        clrs_generated: int = 0,
        updates_undone: int = 0,
        pages_modified: Optional[List[int]] = None,
        records_skipped: int = 0,
        last_processed_lsn: Optional[int] = None,
        completed_transactions: Optional[List[int]] = None,
        remaining_losers: Optional[List[int]] = None,
        is_complete: bool = True,
    ) -> None:
        """Initialize immutable UndoResult."""
        if not isinstance(loser_transactions, int) or isinstance(loser_transactions, bool) or loser_transactions < 0:
            raise UndoError(f"loser_transactions must be a non-negative integer, got {loser_transactions!r}")
        if not isinstance(transactions_undone, int) or isinstance(transactions_undone, bool) or transactions_undone < 0:
            raise UndoError(f"transactions_undone must be a non-negative integer, got {transactions_undone!r}")
        if not isinstance(clrs_generated, int) or isinstance(clrs_generated, bool) or clrs_generated < 0:
            raise UndoError(f"clrs_generated must be a non-negative integer, got {clrs_generated!r}")
        if not isinstance(updates_undone, int) or isinstance(updates_undone, bool) or updates_undone < 0:
            raise UndoError(f"updates_undone must be a non-negative integer, got {updates_undone!r}")
        if not isinstance(records_skipped, int) or isinstance(records_skipped, bool) or records_skipped < 0:
            raise UndoError(f"records_skipped must be a non-negative integer, got {records_skipped!r}")
        if last_processed_lsn is not None and (not isinstance(last_processed_lsn, int) or isinstance(last_processed_lsn, bool) or last_processed_lsn <= 0):
            raise UndoError(f"last_processed_lsn must be a positive integer or None, got {last_processed_lsn!r}")

        self._loser_transactions: int = loser_transactions
        self._transactions_undone: int = transactions_undone
        self._clrs_generated: int = clrs_generated
        self._updates_undone: int = updates_undone
        self._pages_modified: List[int] = sorted(list(set(pages_modified))) if pages_modified is not None else []
        self._records_skipped: int = records_skipped
        self._last_processed_lsn: Optional[int] = last_processed_lsn
        self._completed_transactions: List[int] = sorted(list(set(completed_transactions))) if completed_transactions is not None else []
        self._remaining_losers: List[int] = sorted(list(set(remaining_losers))) if remaining_losers is not None else []
        self._is_complete: bool = bool(is_complete)

    @property
    def loser_transactions(self) -> int:
        """Return the total number of loser transactions identified."""
        return self._loser_transactions

    @property
    def transactions_undone(self) -> int:
        """Return the number of transactions successfully undone."""
        return self._transactions_undone

    @property
    def clrs_generated(self) -> int:
        """Return the number of CLRs generated."""
        return self._clrs_generated

    @property
    def updates_undone(self) -> int:
        """Return the number of UPDATE records undone."""
        return self._updates_undone

    @property
    def pages_modified(self) -> List[int]:
        """Return a copy of the sorted list of unique modified page IDs."""
        return list(self._pages_modified)

    @property
    def records_skipped(self) -> int:
        """Return the number of records skipped."""
        return self._records_skipped

    @property
    def last_processed_lsn(self) -> Optional[int]:
        """Return the most recent/highest LSN processed during Undo."""
        return self._last_processed_lsn

    @property
    def completed_transactions(self) -> List[int]:
        """Return a copy of the sorted list of completed transaction IDs."""
        return list(self._completed_transactions)

    @property
    def remaining_losers(self) -> List[int]:
        """Return a copy of the sorted list of remaining loser transaction IDs."""
        return list(self._remaining_losers)

    @property
    def is_complete(self) -> bool:
        """Return True if all loser transactions were successfully undone."""
        return self._is_complete

    def to_dict(self) -> Dict[str, Any]:
        """Convert undo result to dictionary format."""
        return {
            "loser_transactions": self._loser_transactions,
            "transactions_undone": self._transactions_undone,
            "clrs_generated": self._clrs_generated,
            "updates_undone": self._updates_undone,
            "pages_modified": list(self._pages_modified),
            "records_skipped": self._records_skipped,
            "last_processed_lsn": self._last_processed_lsn,
            "completed_transactions": list(self._completed_transactions),
            "remaining_losers": list(self._remaining_losers),
            "is_complete": self._is_complete,
        }

    def __eq__(self, other: object) -> bool:
        """Equality check."""
        if not isinstance(other, UndoResult):
            return False
        return (
            self._loser_transactions == other._loser_transactions
            and self._transactions_undone == other._transactions_undone
            and self._clrs_generated == other._clrs_generated
            and self._updates_undone == other._updates_undone
            and self._pages_modified == other._pages_modified
            and self._records_skipped == other._records_skipped
            and self._last_processed_lsn == other._last_processed_lsn
            and self._completed_transactions == other._completed_transactions
            and self._remaining_losers == other._remaining_losers
            and self._is_complete == other._is_complete
        )

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"UndoResult(losers={self._loser_transactions}, undone={self._transactions_undone}, "
            f"clrs={self._clrs_generated}, updates_undone={self._updates_undone}, "
            f"pages_modified={self._pages_modified}, complete={self._is_complete})"
        )
