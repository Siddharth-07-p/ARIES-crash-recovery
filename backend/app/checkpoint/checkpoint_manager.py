"""CheckpointManager coordinating checkpoint creation, durability, and discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.checkpoint.checkpoint import CheckpointInfo, CheckpointSnapshot, TransactionTableEntry
from app.checkpoint.exceptions import (
    CheckpointCorruptionError,
    CheckpointError,
    CheckpointManagerClosedError,
    InvalidCheckpointError,
)
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector
from app.wal.log_record import LogRecord, LogRecordType

if TYPE_CHECKING:
    from app.buffer.buffer_manager import BufferManager
    from app.transaction.transaction_manager import TransactionManager
    from app.wal.log_manager import LogManager


class CheckpointManager:
    """Coordinates non-blocking fuzzy checkpointing and master checkpoint discovery.

    Responsibilities:
        - Atomically captures Transaction Table and Dirty Page Table (DPT) state.
        - Writes CHECKPOINT_BEGIN and CHECKPOINT_END records into the WAL.
        - Ensures WAL durability via fsync before reporting checkpoint success.
        - Locates the most recent valid and completed checkpoint from persistent WAL history.
    """

    def __init__(
        self,
        log_manager: LogManager,
        transaction_manager: TransactionManager,
        buffer_manager: BufferManager,
    ) -> None:
        """Initialize CheckpointManager with subsystem managers.

        Args:
            log_manager: LogManager for WAL appending and persistence.
            transaction_manager: TransactionManager for transaction table inspection.
            buffer_manager: BufferManager for Dirty Page Table inspection.

        Raises:
            ValueError: If any required manager is None.
        """
        if log_manager is None:
            raise ValueError("log_manager cannot be None")
        if transaction_manager is None:
            raise ValueError("transaction_manager cannot be None")
        if buffer_manager is None:
            raise ValueError("buffer_manager cannot be None")

        self._log_manager: LogManager = log_manager
        self._transaction_manager: TransactionManager = transaction_manager
        self._buffer_manager: BufferManager = buffer_manager
        self._is_closed: bool = False
        self._latest_checkpoint: Optional[CheckpointInfo] = None

    @property
    def log_manager(self) -> LogManager:
        """Return the underlying LogManager."""
        return self._log_manager

    @property
    def transaction_manager(self) -> TransactionManager:
        """Return the underlying TransactionManager."""
        return self._transaction_manager

    @property
    def buffer_manager(self) -> BufferManager:
        """Return the underlying BufferManager."""
        return self._buffer_manager

    @property
    def is_closed(self) -> bool:
        """Return True if CheckpointManager is closed."""
        return self._is_closed or self._log_manager.is_closed

    def _check_open(self) -> None:
        """Ensure CheckpointManager is open.

        Raises:
            CheckpointManagerClosedError: If the manager is closed.
        """
        if self.is_closed:
            raise CheckpointManagerClosedError("CheckpointManager is closed. No operations permitted.")

    def create_checkpoint(self) -> CheckpointInfo:
        """Create a durable fuzzy checkpoint.

        Execution Steps:
            1. Capture a consistent snapshot of the Transaction Table.
            2. Capture a consistent snapshot of the Dirty Page Table (DPT).
            3. Append CHECKPOINT_BEGIN log record to the WAL.
            4. Append CHECKPOINT_END log record containing the serialized snapshot.
            5. Flush and fsync the WAL so the checkpoint is durably persisted.
            6. Return CheckpointInfo metadata.

        Note:
            Dirty database pages are NOT forced to disk during checkpoint creation.

        Returns:
            CheckpointInfo containing begin_lsn, end_lsn, and snapshot.

        Raises:
            CheckpointManagerClosedError: If the manager is closed.
            CheckpointError: If checkpoint creation or WAL flush fails.
        """
        self._check_open()

        # 1. Capture Transaction Table state
        tx_dict = self._transaction_manager.get_transaction_table()
        tx_entries: Dict[int, TransactionTableEntry] = {}
        for tx_id, tx in tx_dict.items():
            last_lsn = self._log_manager.get_last_lsn(tx_id)
            tx_entries[tx_id] = TransactionTableEntry(
                transaction_id=tx_id,
                state=tx.state,
                last_lsn=last_lsn,
            )

        # 2. Capture Dirty Page Table state
        dpt_dict = self._buffer_manager.dirty_page_table.get_table()

        # 3. Construct deterministic CheckpointSnapshot
        snapshot = CheckpointSnapshot(
            transaction_table=tx_entries,
            dirty_page_table=dpt_dict,
        )

        # 4. Append CHECKPOINT_BEGIN record
        begin_lsn = self._log_manager.append_checkpoint_begin()
        fault_injector.trigger(FaultPoint.CHECKPOINT_BEGIN)

        # 5. Serialize snapshot and append CHECKPOINT_END record
        payload = snapshot.serialize()
        end_lsn = self._log_manager.append_checkpoint_end(
            checkpoint_begin_lsn=begin_lsn,
            payload=payload,
        )

        # 6. Flush WAL to guarantee durability
        self._log_manager.flush()
        fault_injector.trigger(FaultPoint.CHECKPOINT_END)

        checkpoint_info = CheckpointInfo(
            begin_lsn=begin_lsn,
            end_lsn=end_lsn,
            snapshot=snapshot,
        )
        self._latest_checkpoint = checkpoint_info
        return checkpoint_info

    def get_latest_checkpoint(self) -> Optional[CheckpointInfo]:
        """Locate and return the most recent completed and valid checkpoint.

        Scans the WAL to identify the latest completed CHECKPOINT_END record
        with a valid matching CHECKPOINT_BEGIN record and valid snapshot payload.

        Incomplete checkpoints (e.g. CHECKPOINT_BEGIN without CHECKPOINT_END)
        are ignored.

        Returns:
            CheckpointInfo instance if a completed checkpoint exists, or None.

        Raises:
            CheckpointManagerClosedError: If manager is closed.
            CheckpointCorruptionError: If checkpoint payload is corrupt.
        """
        self._check_open()

        records = self._log_manager.read_all()
        records_by_lsn: Dict[int, LogRecord] = {rec.lsn: rec for rec in records}

        # Search backward from the latest log record
        for rec in reversed(records):
            if rec.record_type == LogRecordType.CHECKPOINT_END:
                begin_lsn = rec.checkpoint_begin_lsn
                if begin_lsn is None or begin_lsn not in records_by_lsn:
                    # Incomplete / invalid checkpoint reference, continue backward
                    continue

                begin_rec = records_by_lsn[begin_lsn]
                if begin_rec.record_type != LogRecordType.CHECKPOINT_BEGIN:
                    # Invalid begin record type
                    continue

                if rec.checkpoint_payload is None:
                    raise CheckpointCorruptionError(
                        f"CHECKPOINT_END at LSN {rec.lsn} has missing snapshot payload"
                    )

                snapshot = CheckpointSnapshot.deserialize(rec.checkpoint_payload)
                return CheckpointInfo(
                    begin_lsn=begin_lsn,
                    end_lsn=rec.lsn,
                    snapshot=snapshot,
                )

        return None

    def locate_latest_checkpoint(self) -> Optional[CheckpointInfo]:
        """Convenience alias for get_latest_checkpoint."""
        return self.get_latest_checkpoint()

    def close(self) -> None:
        """Close the CheckpointManager."""
        self._is_closed = True

    def __enter__(self) -> "CheckpointManager":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()

    def __repr__(self) -> str:
        """String representation."""
        status = "closed" if self.is_closed else "open"
        return f"CheckpointManager(status='{status}')"
