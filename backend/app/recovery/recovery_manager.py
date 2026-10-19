"""RecoveryManager coordinating the complete ARIES crash recovery workflow."""

from pathlib import Path
from typing import Any, Optional, Union

from app.buffer.buffer_manager import BufferManager
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector
from app.recovery.analysis import Analysis
from app.recovery.exceptions import RecoveryManagerError
from app.recovery.recovery_state import (
    AnalysisResult,
    RecoveryResult,
    RedoResult,
    UndoResult,
)
from app.recovery.redo import Redo
from app.recovery.undo import Undo
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager


class RecoveryManager:
    """Coordinates the complete ARIES crash recovery workflow.

    Executes the three canonical ARIES recovery phases in strict sequence:
        1. Analysis: Scans the WAL forward from the latest checkpoint (or beginning of WAL)
           to reconstruct the active Transaction Table (identifying winners and losers)
           and the Dirty Page Table (DPT) with initial recLSNs.
        2. Redo: Scans the WAL forward starting from redo_start_lsn (the minimum recLSN in DPT)
           and repeats history by replaying all eligible UPDATE and CLR records to restore
           the database buffer and disk state to the exact moment of crash.
        3. Undo: Scans the WAL backward to roll back active loser transactions in global
           reverse-LSN order, applying before-images, generating Compensation Log Records (CLRs),
           maintaining WAL-before-page durability, and logging completion ABORT records.
        4. Final verification and persistence: Ensures all modified pages are flushed and
           constructs an immutable, structured RecoveryResult.
    """

    def __init__(
        self,
        log_manager: LogManager,
        buffer_manager: BufferManager,
        checkpoint_manager: Optional[CheckpointManager] = None,
    ) -> None:
        """Initialize RecoveryManager with subsystem managers.

        Args:
            log_manager: Persistent LogManager for WAL inspection and CLR/ABORT logging.
            buffer_manager: BufferManager for page fetch, replay, mutation, and persistence.
            checkpoint_manager: Optional CheckpointManager for checkpoint discovery.

        Raises:
            RecoveryManagerError: If log_manager or buffer_manager is None or closed.
        """
        if log_manager is None:
            raise RecoveryManagerError("log_manager cannot be None")
        if not isinstance(log_manager, LogManager):
            raise RecoveryManagerError(f"log_manager must be LogManager, got {type(log_manager).__name__}")
        if log_manager.is_closed:
            raise RecoveryManagerError("log_manager cannot be closed")

        if buffer_manager is None:
            raise RecoveryManagerError("buffer_manager cannot be None")
        if not isinstance(buffer_manager, BufferManager):
            raise RecoveryManagerError(f"buffer_manager must be BufferManager, got {type(buffer_manager).__name__}")
        if buffer_manager.is_closed:
            raise RecoveryManagerError("buffer_manager cannot be closed")

        if checkpoint_manager is not None:
            if not isinstance(checkpoint_manager, CheckpointManager):
                raise RecoveryManagerError(
                    f"checkpoint_manager must be CheckpointManager, got {type(checkpoint_manager).__name__}"
                )
            if checkpoint_manager.is_closed:
                raise RecoveryManagerError("checkpoint_manager cannot be closed")

        self._log_manager: LogManager = log_manager
        self._buffer_manager: BufferManager = buffer_manager
        self._checkpoint_manager: Optional[CheckpointManager] = checkpoint_manager

        self._last_result: Optional[RecoveryResult] = None
        self._is_recovered: bool = False

    @property
    def log_manager(self) -> LogManager:
        """Return the underlying LogManager."""
        return self._log_manager

    @property
    def buffer_manager(self) -> BufferManager:
        """Return the underlying BufferManager."""
        return self._buffer_manager

    @property
    def checkpoint_manager(self) -> Optional[CheckpointManager]:
        """Return the underlying CheckpointManager, if any."""
        return self._checkpoint_manager

    @property
    def last_result(self) -> Optional[RecoveryResult]:
        """Return the result of the most recent recovery operation, if any."""
        return self._last_result

    @property
    def is_recovered(self) -> bool:
        """Return True if recovery completed successfully."""
        return self._is_recovered

    def recover(
        self_or_lm: Any,
        buffer_manager: Optional[BufferManager] = None,
        checkpoint_manager: Optional[CheckpointManager] = None,
        raise_on_error: bool = True,
    ) -> RecoveryResult:
        """Execute the complete ARIES recovery workflow.

        Supports both:
            - Instance method: manager.recover(raise_on_error=True)
            - Class method: RecoveryManager.recover(log_manager, buffer_manager, checkpoint_manager=None, raise_on_error=True)

        Args:
            self_or_lm: RecoveryManager instance (if instance method) or LogManager (if classmethod).
            buffer_manager: BufferManager instance (if classmethod).
            checkpoint_manager: Optional CheckpointManager (if classmethod).
            raise_on_error: If True (default), re-raises any exception encountered during recovery.
                            If False, captures the error and returns a failed RecoveryResult.

        Returns:
            Structured immutable RecoveryResult summarizing each phase's outcome.

        Raises:
            Exception: If any recovery phase fails and raise_on_error is True.
        """
        if isinstance(self_or_lm, RecoveryManager):
            manager = self_or_lm
            target_raise = raise_on_error
        else:
            log_manager = self_or_lm
            manager = RecoveryManager(
                log_manager=log_manager,
                buffer_manager=buffer_manager,  # type: ignore
                checkpoint_manager=checkpoint_manager,
            )
            target_raise = raise_on_error

        return manager._execute_recovery(raise_on_error=target_raise)

    def run(self, raise_on_error: bool = True) -> RecoveryResult:
        """Alias for recover()."""
        return self.recover(raise_on_error=raise_on_error)

    def _execute_recovery(self, raise_on_error: bool = True) -> RecoveryResult:
        """Internal recovery orchestration coordinating Analysis, Redo, and Undo in order."""
        current_stage = "INITIALIZATION"
        analysis_res: Optional[AnalysisResult] = None
        redo_res: Optional[RedoResult] = None
        undo_res: Optional[UndoResult] = None

        try:
            # -----------------------------------------------------------------
            # Stage 1: Analysis Phase
            # -----------------------------------------------------------------
            current_stage = "ANALYSIS"
            analysis = Analysis(
                log_manager=self._log_manager,
                checkpoint_manager=self._checkpoint_manager,
            )
            analysis_res = analysis.execute()

            # Crash injection point after Analysis
            fault_injector.trigger(FaultPoint.AFTER_ANALYSIS_BEFORE_REDO)

            # -----------------------------------------------------------------
            # Stage 2: Redo Phase (Repeating History)
            # -----------------------------------------------------------------
            current_stage = "REDO"
            redo = Redo(
                log_manager=self._log_manager,
                buffer_manager=self._buffer_manager,
            )
            redo_res = redo.execute(analysis_res)

            # Crash injection point after Redo
            fault_injector.trigger(FaultPoint.AFTER_REDO_BEFORE_UNDO)

            # -----------------------------------------------------------------
            # Stage 3: Undo Phase (Rolling back active losers)
            # -----------------------------------------------------------------
            current_stage = "UNDO"
            undo = Undo(
                log_manager=self._log_manager,
                buffer_manager=self._buffer_manager,
            )
            undo_res = undo.execute(analysis_res)

            # Crash injection point after Undo
            fault_injector.trigger(FaultPoint.AFTER_UNDO_BEFORE_COMPLETE)

            # -----------------------------------------------------------------
            # Stage 4: Final verification and persistence
            # -----------------------------------------------------------------
            current_stage = "VERIFICATION"

            # Flush any remaining dirty pages modified during recovery to disk
            self._buffer_manager.flush_all_pages()

            # Ensure all loser transactions completed undo cleanly
            if not undo_res.is_complete or len(undo_res.remaining_losers) > 0:
                raise RecoveryManagerError(
                    f"Undo phase ended with remaining loser transactions: {undo_res.remaining_losers}"
                )

            # Construct successful RecoveryResult
            result = RecoveryResult(
                is_success=True,
                analysis_result=analysis_res,
                redo_result=redo_res,
                undo_result=undo_res,
                error_message=None,
                stage="COMPLETE",
            )
            self._last_result = result
            self._is_recovered = True
            return result

        except Exception as exc:
            self._is_recovered = False
            failed_stage = f"FAILED_AT_{current_stage}"
            failed_result = RecoveryResult(
                is_success=False,
                analysis_result=analysis_res,
                redo_result=redo_res,
                undo_result=undo_res,
                error_message=str(exc),
                stage=failed_stage,
            )
            self._last_result = failed_result

            if raise_on_error:
                raise
            return failed_result

    @classmethod
    def recover_from_files(
        cls,
        db_path: Union[str, Path],
        wal_path: Union[str, Path],
        pool_size: int = 10,
        checkpoint_manager: Optional[CheckpointManager] = None,
        raise_on_error: bool = True,
    ) -> RecoveryResult:
        """Convenience method opening existing database and WAL files, executing recovery, and closing managers.

        Args:
            db_path: Filepath to the existing database file.
            wal_path: Filepath to the existing WAL file.
            pool_size: Buffer pool size.
            checkpoint_manager: Optional CheckpointManager.
            raise_on_error: If True, re-raises any error encountered.

        Returns:
            RecoveryResult summarizing the recovery outcome.
        """
        with DiskManager(db_path=db_path) as dm, LogManager(wal_path=wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=pool_size, log_manager=lm)
            try:
                manager = cls(
                    log_manager=lm,
                    buffer_manager=bm,
                    checkpoint_manager=checkpoint_manager,
                )
                result = manager.recover(raise_on_error=raise_on_error)
                bm.flush_all_pages()
                return result
            finally:
                bm.close(flush_dirty=False)

    def __repr__(self) -> str:
        """String representation of RecoveryManager."""
        status = "recovered" if self._is_recovered else "unrecovered"
        return f"RecoveryManager(status='{status}', last_result={self._last_result})"
