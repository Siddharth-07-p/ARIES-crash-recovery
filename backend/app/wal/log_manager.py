"""LogManager responsible for WAL file lifecycle, LSN allocation, and persistence."""

import os
from pathlib import Path
from typing import Dict, List, Optional, Union

from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector
from app.wal.constants import DEFAULT_WAL_PATH
from app.wal.exceptions import (
    InvalidLSNError,
    InvalidLogRecordError,
    LogCorruptionError,
    LogManagerClosedError,
    WALFlushError,
)
from app.wal.log_record import LogRecord, LogRecordType


class LogManager:
    """Manages Write-Ahead Log records, persistent WAL file, and durability boundary."""

    def __init__(
        self,
        wal_path: Union[str, Path] = DEFAULT_WAL_PATH,
        auto_flush: bool = False,
    ) -> None:
        """Initialize the LogManager.

        Args:
            wal_path: Filepath for the WAL file.
            auto_flush: If True, automatically flushes and fsyncs on every log append.

        Raises:
            LogCorruptionError: If the existing WAL file contains corrupt records.
            LogManagerError: If file creation or initialization fails.
        """
        self._wal_path: Path = Path(wal_path)
        self._auto_flush: bool = auto_flush
        self._is_closed: bool = False

        # In-memory tracking
        self._records: Dict[int, LogRecord] = {}
        self._tx_last_lsn: Dict[int, int] = {}
        self._next_lsn: int = 1
        self._flushed_lsn: int = 0

        # Ensure parent directories exist
        if self._wal_path.parent:
            self._wal_path.parent.mkdir(parents=True, exist_ok=True)

        # Open WAL file in append+read binary mode
        try:
            self._file = open(self._wal_path, "a+b")
        except OSError as exc:
            raise WALFlushError(f"Failed to open WAL file '{self._wal_path}': {exc}") from exc

        # Read existing records from WAL to initialize state and next LSN
        self._load_existing_wal()

    @property
    def wal_path(self) -> Path:
        """Return the WAL filepath."""
        return self._wal_path

    @property
    def is_closed(self) -> bool:
        """Return True if the LogManager is closed."""
        return self._is_closed

    @property
    def flushed_lsn(self) -> int:
        """Return the highest LSN that has been flushed and fsynced to disk."""
        return self._flushed_lsn

    @property
    def next_lsn(self) -> int:
        """Return the next LSN that will be allocated."""
        return self._next_lsn

    def _check_open(self) -> None:
        """Verify that the LogManager is open.

        Raises:
            LogManagerClosedError: If the manager is closed.
        """
        if self._is_closed:
            raise LogManagerClosedError("LogManager is closed. No operations permitted.")

    def _load_existing_wal(self) -> None:
        """Read and validate all existing log records from the WAL file."""
        self._file.seek(0, os.SEEK_SET)
        data = self._file.read()

        offset = 0
        total_bytes = len(data)
        max_lsn = 0

        while offset < total_bytes:
            record, consumed = LogRecord.deserialize(data, offset)
            self._records[record.lsn] = record

            if record.transaction_id is not None:
                self._tx_last_lsn[record.transaction_id] = record.lsn

            if record.lsn > max_lsn:
                max_lsn = record.lsn

            offset += consumed

        if max_lsn > 0:
            self._next_lsn = max_lsn + 1
            self._flushed_lsn = max_lsn
        else:
            self._next_lsn = 1
            self._flushed_lsn = 0

        # Position write pointer at the end of the file
        self._file.seek(0, os.SEEK_END)

    def append_log(self, record: LogRecord) -> int:
        """Append a pre-constructed LogRecord to the WAL.

        Args:
            record: LogRecord to append.

        Returns:
            The allocated LSN.

        Raises:
            LogManagerClosedError: If manager is closed.
            InvalidLSNError: If record LSN does not match expected next LSN.
        """
        self._check_open()

        if record.lsn != self._next_lsn:
            raise InvalidLSNError(
                f"Record LSN {record.lsn} does not match expected monotonic next LSN {self._next_lsn}"
            )

        serialized = record.serialize()
        try:
            self._file.seek(0, os.SEEK_END)
            self._file.write(serialized)
            self._file.flush()
        except OSError as exc:
            raise WALFlushError(f"Failed writing to WAL file: {exc}") from exc

        self._records[record.lsn] = record
        if record.transaction_id is not None:
            self._tx_last_lsn[record.transaction_id] = record.lsn

        allocated_lsn = self._next_lsn
        self._next_lsn += 1

        if record.record_type == LogRecordType.UPDATE:
            fault_injector.trigger(FaultPoint.AFTER_LOG_APPEND_BEFORE_WAL_FLUSH)

        if self._auto_flush:
            self.flush()

        return allocated_lsn

    def append_begin(self, transaction_id: int) -> int:
        """Append a BEGIN log record for a transaction.

        Args:
            transaction_id: Unique positive transaction identifier.

        Returns:
            The allocated LSN.
        """
        self._check_open()
        lsn = self._next_lsn
        record = LogRecord(
            lsn=lsn,
            record_type=LogRecordType.BEGIN,
            transaction_id=transaction_id,
            prev_lsn=None,
        )
        return self.append_log(record)

    def append_commit(self, transaction_id: int) -> int:
        """Append a COMMIT log record for a transaction.

        Args:
            transaction_id: Unique positive transaction identifier.

        Returns:
            The allocated LSN.
        """
        self._check_open()
        lsn = self._next_lsn
        prev_lsn = self._tx_last_lsn.get(transaction_id)
        record = LogRecord(
            lsn=lsn,
            record_type=LogRecordType.COMMIT,
            transaction_id=transaction_id,
            prev_lsn=prev_lsn,
        )
        return self.append_log(record)

    def append_abort(self, transaction_id: int) -> int:
        """Append an ABORT log record for a transaction.

        Args:
            transaction_id: Unique positive transaction identifier.

        Returns:
            The allocated LSN.
        """
        self._check_open()
        lsn = self._next_lsn
        prev_lsn = self._tx_last_lsn.get(transaction_id)
        record = LogRecord(
            lsn=lsn,
            record_type=LogRecordType.ABORT,
            transaction_id=transaction_id,
            prev_lsn=prev_lsn,
        )
        return self.append_log(record)

    def append_update(
        self,
        transaction_id: int,
        page_id: int,
        offset: int,
        before_image: Union[bytes, bytearray, memoryview],
        after_image: Union[bytes, bytearray, memoryview],
    ) -> int:
        """Append an UPDATE log record containing before/after images.

        Args:
            transaction_id: Unique positive transaction identifier.
            page_id: Target page ID modified.
            offset: Byte offset within the page.
            before_image: Raw bytes prior to update.
            after_image: Raw bytes after update.

        Returns:
            The allocated LSN.
        """
        self._check_open()
        lsn = self._next_lsn
        prev_lsn = self._tx_last_lsn.get(transaction_id)
        record = LogRecord(
            lsn=lsn,
            record_type=LogRecordType.UPDATE,
            transaction_id=transaction_id,
            prev_lsn=prev_lsn,
            page_id=page_id,
            offset=offset,
            before_image=before_image,
            after_image=after_image,
        )
        return self.append_log(record)

    def append_checkpoint_begin(self) -> int:
        """Append a CHECKPOINT_BEGIN log record.

        Returns:
            The allocated LSN.
        """
        self._check_open()
        lsn = self._next_lsn
        record = LogRecord(
            lsn=lsn,
            record_type=LogRecordType.CHECKPOINT_BEGIN,
        )
        return self.append_log(record)

    def append_checkpoint_end(
        self,
        checkpoint_begin_lsn: int,
        payload: Optional[Union[bytes, bytearray, memoryview]] = None,
    ) -> int:
        """Append a CHECKPOINT_END log record referencing the begin LSN and containing snapshot payload.

        Args:
            checkpoint_begin_lsn: LSN of the corresponding CHECKPOINT_BEGIN record.
            payload: Deterministic binary serialization of the checkpoint snapshot.

        Returns:
            The allocated LSN.
        """
        self._check_open()
        if not isinstance(checkpoint_begin_lsn, int) or isinstance(checkpoint_begin_lsn, bool) or checkpoint_begin_lsn <= 0:
            raise InvalidLSNError(f"checkpoint_begin_lsn must be a positive integer, got {checkpoint_begin_lsn!r}")

        lsn = self._next_lsn
        record = LogRecord(
            lsn=lsn,
            record_type=LogRecordType.CHECKPOINT_END,
            prev_lsn=checkpoint_begin_lsn,
            checkpoint_payload=payload,
        )
        return self.append_log(record)

    def flush(self) -> int:
        """Flush Python file buffers and execute os.fsync to ensure WAL durability.

        Returns:
            The highest flushed LSN.

        Raises:
            LogManagerClosedError: If manager is closed.
            WALFlushError: If file flush or fsync fails.
        """
        self._check_open()
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._flushed_lsn = self._next_lsn - 1
            return self._flushed_lsn
        except OSError as exc:
            raise WALFlushError(f"Failed to flush/fsync WAL file: {exc}") from exc

    def flush_to_lsn(self, target_lsn: int) -> int:
        """Ensure WAL records up to and including target_lsn are durable on disk.

        Args:
            target_lsn: LSN threshold to flush through.

        Returns:
            The current flushed LSN.
        """
        self._check_open()
        if not isinstance(target_lsn, int) or isinstance(target_lsn, bool) or target_lsn < 0:
            raise InvalidLSNError(f"target_lsn must be a non-negative integer, got {target_lsn!r}")

        if target_lsn > self._flushed_lsn:
            self.flush()

        return self._flushed_lsn

    def read_all(self) -> List[LogRecord]:
        """Read all log records in sequential order.

        Returns:
            List of LogRecord instances ordered by LSN.
        """
        self._check_open()
        return [self._records[lsn] for lsn in sorted(self._records.keys())]

    def read_from(self, lsn: int) -> List[LogRecord]:
        """Read log records sequentially starting from a specific LSN (inclusive).

        Args:
            lsn: Starting Log Sequence Number.

        Returns:
            List of LogRecord instances where record.lsn >= lsn.
        """
        self._check_open()
        if not isinstance(lsn, int) or isinstance(lsn, bool) or lsn <= 0:
            raise InvalidLSNError(f"LSN must be a positive integer, got {lsn!r}")

        return [self._records[k] for k in sorted(self._records.keys()) if k >= lsn]

    def get_log_record(self, lsn: int) -> Optional[LogRecord]:
        """Retrieve a single log record by LSN.

        Args:
            lsn: Target Log Sequence Number.

        Returns:
            LogRecord instance if found, or None.
        """
        self._check_open()
        if not isinstance(lsn, int) or isinstance(lsn, bool) or lsn <= 0:
            raise InvalidLSNError(f"LSN must be a positive integer, got {lsn!r}")

        return self._records.get(lsn)

    def get_last_lsn(self, transaction_id: int) -> Optional[int]:
        """Return the most recent LSN for the specified transaction.

        Args:
            transaction_id: Target transaction ID.

        Returns:
            Last LSN recorded for the transaction, or None if no records exist.
        """
        self._check_open()
        return self._tx_last_lsn.get(transaction_id)

    def close(self, flush: bool = True) -> None:
        """Close the LogManager, optionally flushing unwritten records.

        Args:
            flush: If True, flushes and fsyncs pending records before closing.
        """
        if not self._is_closed:
            if flush and hasattr(self, "_file") and not self._file.closed:
                try:
                    self.flush()
                except Exception:
                    pass
            if hasattr(self, "_file") and not self._file.closed:
                try:
                    self._file.close()
                except Exception:
                    pass
            self._is_closed = True

    def __enter__(self) -> "LogManager":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close(flush=True)

    def __repr__(self) -> str:
        """String representation of LogManager."""
        status = "closed" if self._is_closed else "open"
        return (
            f"LogManager(path='{self._wal_path}', total_records={len(self._records)}, "
            f"flushed_lsn={self._flushed_lsn}, next_lsn={self._next_lsn}, status='{status}')"
        )
