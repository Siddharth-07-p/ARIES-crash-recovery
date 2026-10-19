"""Constants and enumeration of named fault points for crash injection."""

from enum import Enum

# Default process termination exit code for simulated crashes
DEFAULT_CRASH_EXIT_CODE = 42


class FaultPoint(str, Enum):
    """Named fault points representing deterministic failure boundaries during normal database operations."""

    # Triggered after an UPDATE log record is appended to the WAL buffer, before fsync/flush
    AFTER_LOG_APPEND_BEFORE_WAL_FLUSH = "AFTER_LOG_APPEND_BEFORE_WAL_FLUSH"

    # Triggered after WAL is flushed to disk, but before the corresponding dirty page is written to disk
    AFTER_WAL_FLUSH_BEFORE_PAGE_FLUSH = "AFTER_WAL_FLUSH_BEFORE_PAGE_FLUSH"

    # Triggered during transaction commit processing (after commit log record is appended/flushed)
    DURING_TRANSACTION_COMMIT = "DURING_TRANSACTION_COMMIT"

    # Triggered during transaction abort processing
    DURING_TRANSACTION_ABORT = "DURING_TRANSACTION_ABORT"

    # Triggered after CHECKPOINT_BEGIN is written, before CHECKPOINT_END
    CHECKPOINT_BEGIN = "CHECKPOINT_BEGIN"

    # Triggered after CHECKPOINT_END is written and flushed
    CHECKPOINT_END = "CHECKPOINT_END"

    # Triggered during recovery Undo before the CLR is flushed to disk
    DURING_UNDO_BEFORE_CLR_FLUSH = "DURING_UNDO_BEFORE_CLR_FLUSH"

    # Triggered during recovery Undo after CLR is flushed to disk, but before modified page is flushed
    DURING_UNDO_AFTER_CLR_FLUSH_BEFORE_PAGE_FLUSH = "DURING_UNDO_AFTER_CLR_FLUSH_BEFORE_PAGE_FLUSH"

    # Triggered during recovery Undo after the modified page is flushed to disk
    DURING_UNDO_AFTER_PAGE_FLUSH = "DURING_UNDO_AFTER_PAGE_FLUSH"

    # Triggered during recovery orchestration after Analysis completes, before Redo begins
    AFTER_ANALYSIS_BEFORE_REDO = "AFTER_ANALYSIS_BEFORE_REDO"

    # Triggered during recovery orchestration after Redo completes, before Undo begins
    AFTER_REDO_BEFORE_UNDO = "AFTER_REDO_BEFORE_UNDO"

    # Triggered during recovery orchestration after Undo completes, before final completion verification
    AFTER_UNDO_BEFORE_COMPLETE = "AFTER_UNDO_BEFORE_COMPLETE"
