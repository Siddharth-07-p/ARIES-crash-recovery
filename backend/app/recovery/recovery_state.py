"""Data structures representing reconstructed recovery state from ARIES Analysis."""

from typing import Any, Dict, List, Optional, Union
from app.checkpoint.checkpoint import CheckpointInfo
from app.recovery.exceptions import AnalysisError
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
