"""ARIES Analysis Phase implementation for database crash recovery."""

from typing import Dict, List, Optional
from app.checkpoint.checkpoint import CheckpointInfo, CheckpointSnapshot
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.checkpoint.exceptions import CheckpointCorruptionError
from app.recovery.exceptions import AnalysisError
from app.recovery.recovery_state import AnalysisResult, RecoveredTransaction
from app.transaction.transaction import TransactionState
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecord, LogRecordType


class Analysis:
    """Executes the ARIES Analysis recovery phase.

    Analysis inspects the persistent WAL and the latest valid checkpoint
    to reconstruct the state of transactions and dirty pages at the time of crash.

    Key Outputs:
        - Reconstructed Transaction Table (with last_lsn per transaction).
        - Reconstructed Dirty Page Table (with recLSN per dirty page).
        - Winner Transactions (COMMITTED).
        - Loser Transactions (RUNNING at crash time).
        - Redo Starting LSN (min recLSN in DPT, or None if DPT is empty).
    """

    def __init__(
        self,
        log_manager: LogManager,
        checkpoint_manager: Optional[CheckpointManager] = None,
    ) -> None:
        """Initialize Analysis with a LogManager and optional CheckpointManager.

        Args:
            log_manager: Persistent LogManager instance to scan WAL records from.
            checkpoint_manager: Optional CheckpointManager for checkpoint discovery.

        Raises:
            AnalysisError: If log_manager is None.
        """
        if log_manager is None:
            raise AnalysisError("log_manager cannot be None")

        self._log_manager: LogManager = log_manager
        self._checkpoint_manager: Optional[CheckpointManager] = checkpoint_manager

    def _discover_latest_checkpoint(self) -> Optional[CheckpointInfo]:
        """Locate the latest valid and completed checkpoint.

        Uses the provided CheckpointManager if available, or scans the WAL directly.
        Incomplete checkpoints are safely ignored.
        """
        if self._checkpoint_manager is not None and not self._checkpoint_manager.is_closed:
            return self._checkpoint_manager.get_latest_checkpoint()

        # Direct WAL scan for checkpoint if CheckpointManager is not supplied
        records = self._log_manager.read_all()
        records_by_lsn: Dict[int, LogRecord] = {rec.lsn: rec for rec in records}

        for rec in reversed(records):
            if rec.record_type == LogRecordType.CHECKPOINT_END:
                begin_lsn = rec.checkpoint_begin_lsn
                if begin_lsn is None or begin_lsn not in records_by_lsn:
                    continue

                begin_rec = records_by_lsn[begin_lsn]
                if begin_rec.record_type != LogRecordType.CHECKPOINT_BEGIN:
                    continue

                if rec.checkpoint_payload is None:
                    continue

                try:
                    snapshot = CheckpointSnapshot.deserialize(rec.checkpoint_payload)
                    return CheckpointInfo(
                        begin_lsn=begin_lsn,
                        end_lsn=rec.lsn,
                        snapshot=snapshot,
                    )
                except CheckpointCorruptionError:
                    continue

        return None

    def execute(self) -> AnalysisResult:
        """Execute the ARIES Analysis algorithm and return the reconstructed recovery state."""
        checkpoint = self._discover_latest_checkpoint()

        tx_table: Dict[int, RecoveredTransaction] = {}
        dpt: Dict[int, int] = {}
        analysis_start_lsn: Optional[int] = None
        last_analyzed_lsn: Optional[int] = None

        if checkpoint is not None:
            # 1. Initialize from checkpoint snapshot
            for tx_id, entry in checkpoint.snapshot.transaction_table.items():
                tx_table[tx_id] = RecoveredTransaction(
                    transaction_id=entry.transaction_id,
                    state=entry.state,
                    last_lsn=entry.last_lsn,
                )

            for page_id, rec_lsn in checkpoint.snapshot.dirty_page_table.items():
                dpt[page_id] = rec_lsn

            analysis_start_lsn = checkpoint.begin_lsn
            # Scan forward from the CHECKPOINT_BEGIN record
            records = self._log_manager.read_from(checkpoint.begin_lsn)
        else:
            # 2. No checkpoint: start from beginning of WAL
            records = self._log_manager.read_all()
            if records:
                analysis_start_lsn = records[0].lsn

        # 3. Forward scan through log records in ascending LSN order
        for rec in records:
            last_analyzed_lsn = rec.lsn
            rec_type = rec.record_type
            tx_id = rec.transaction_id

            if rec_type == LogRecordType.BEGIN:
                assert tx_id is not None
                tx_table[tx_id] = RecoveredTransaction(
                    transaction_id=tx_id,
                    state=TransactionState.RUNNING,
                    last_lsn=rec.lsn,
                )

            elif rec_type == LogRecordType.UPDATE:
                assert tx_id is not None
                assert rec.page_id is not None

                # Update or register transaction in Transaction Table
                if tx_id in tx_table:
                    tx_table[tx_id].last_lsn = rec.lsn
                else:
                    tx_table[tx_id] = RecoveredTransaction(
                        transaction_id=tx_id,
                        state=TransactionState.RUNNING,
                        last_lsn=rec.lsn,
                    )

                # Update Dirty Page Table: record recLSN on first dirtying
                page_id = rec.page_id
                if page_id not in dpt:
                    dpt[page_id] = rec.lsn

            elif rec_type == LogRecordType.COMMIT:
                assert tx_id is not None
                if tx_id in tx_table:
                    tx_table[tx_id].state = TransactionState.COMMITTED
                    tx_table[tx_id].last_lsn = rec.lsn
                else:
                    tx_table[tx_id] = RecoveredTransaction(
                        transaction_id=tx_id,
                        state=TransactionState.COMMITTED,
                        last_lsn=rec.lsn,
                    )

            elif rec_type == LogRecordType.ABORT:
                assert tx_id is not None
                if tx_id in tx_table:
                    tx_table[tx_id].state = TransactionState.ABORTED
                    tx_table[tx_id].last_lsn = rec.lsn
                else:
                    tx_table[tx_id] = RecoveredTransaction(
                        transaction_id=tx_id,
                        state=TransactionState.ABORTED,
                        last_lsn=rec.lsn,
                    )

            elif rec_type in (LogRecordType.CHECKPOINT_BEGIN, LogRecordType.CHECKPOINT_END):
                # Checkpoint records do not modify transaction/page state during forward scan
                pass

        # 4. Partition transactions into winners (COMMITTED) and losers (RUNNING)
        winners = sorted([
            tx_id for tx_id, tx in tx_table.items() if tx.state == TransactionState.COMMITTED
        ])
        losers = sorted([
            tx_id for tx_id, tx in tx_table.items() if tx.state == TransactionState.RUNNING
        ])

        # 5. Determine Redo starting LSN: smallest recLSN in DPT, or None if DPT is empty
        redo_start_lsn = min(dpt.values()) if dpt else None

        return AnalysisResult(
            transaction_table=tx_table,
            dirty_page_table=dpt,
            winners=winners,
            losers=losers,
            redo_start_lsn=redo_start_lsn,
            analysis_start_lsn=analysis_start_lsn,
            last_analyzed_lsn=last_analyzed_lsn,
            checkpoint_info=checkpoint,
        )

    def run(self) -> AnalysisResult:
        """Alias for execute()."""
        return self.execute()

    @classmethod
    def analyze(
        cls,
        log_manager: LogManager,
        checkpoint_manager: Optional[CheckpointManager] = None,
    ) -> AnalysisResult:
        """Classmethod convenience entry point for running Analysis."""
        instance = cls(log_manager=log_manager, checkpoint_manager=checkpoint_manager)
        return instance.execute()
