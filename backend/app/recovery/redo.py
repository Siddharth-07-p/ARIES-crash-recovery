"""ARIES Redo Phase implementation for database crash recovery."""

from typing import Optional, Set
from app.buffer.buffer_manager import BufferManager
from app.recovery.exceptions import RedoError
from app.recovery.recovery_state import AnalysisResult, RedoResult
from app.storage.constants import PAGE_SIZE
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecordType


class Redo:
    """Executes the ARIES Redo recovery phase.

    Redo repeats history by scanning the WAL forward starting from redo_start_lsn
    (the minimum recLSN in the reconstructed Dirty Page Table) and replaying
    all eligible UPDATE records.

    Key Principles:
        - Repeats history: replays updates for BOTH committed (winners) and
          uncommitted (losers) transactions.
        - DPT Filtering: skips updates for pages not present in the reconstructed DPT.
        - recLSN Filtering: skips updates where record.lsn < DPT[page_id].
        - Page-LSN Rule: skips updates where page.page_lsn >= record.lsn.
        - Idempotent: re-running Redo will skip already-applied records.
        - Safe boundaries: validates offset and payload size against PAGE_SIZE.
    """

    def __init__(
        self,
        log_manager: LogManager,
        buffer_manager: BufferManager,
    ) -> None:
        """Initialize Redo with a LogManager and BufferManager.

        Args:
            log_manager: Persistent LogManager instance to scan WAL records from.
            buffer_manager: BufferManager instance to apply and persist page updates.

        Raises:
            RedoError: If log_manager or buffer_manager is None.
        """
        if log_manager is None:
            raise RedoError("log_manager cannot be None")
        if buffer_manager is None:
            raise RedoError("buffer_manager cannot be None")

        self._log_manager: LogManager = log_manager
        self._buffer_manager: BufferManager = buffer_manager

    def execute(self, analysis_result: AnalysisResult) -> RedoResult:
        """Execute the ARIES Redo algorithm on the reconstructed analysis state.

        Args:
            analysis_result: Reconstructed recovery state from the Analysis phase.

        Returns:
            RedoResult summarizing scanned, redone, and skipped records.

        Raises:
            RedoError: If analysis_result is invalid or closed managers are used.
        """
        if analysis_result is None or not isinstance(analysis_result, AnalysisResult):
            raise RedoError("analysis_result must be a valid AnalysisResult instance")

        redo_start_lsn = analysis_result.redo_start_lsn
        if redo_start_lsn is None:
            # DPT is empty: no pages need Redo, do not scan WAL
            return RedoResult(
                redo_start_lsn=None,
                last_scanned_lsn=None,
                records_scanned=0,
                records_redone=0,
                records_skipped=0,
                pages_modified=[],
                pages_skipped=[],
            )

        dpt = analysis_result.dirty_page_table
        records = self._log_manager.read_from(redo_start_lsn)

        records_scanned = 0
        records_redone = 0
        records_skipped = 0
        last_scanned_lsn: Optional[int] = None
        pages_modified: Set[int] = set()
        pages_skipped: Set[int] = set()

        for record in records:
            records_scanned += 1
            last_scanned_lsn = record.lsn

            # 1. UPDATE and CLR records are candidates for Redo
            if record.record_type not in (LogRecordType.UPDATE, LogRecordType.CLR):
                records_skipped += 1
                continue

            page_id = record.page_id
            if page_id is None:
                raise RedoError(f"{record.record_type.value} record at LSN {record.lsn} is missing page_id")

            # 2. DPT filtering: check if page_id exists in DPT
            if page_id not in dpt:
                records_skipped += 1
                pages_skipped.add(page_id)
                continue

            # 3. recLSN filtering: skip if record.lsn < recLSN
            rec_lsn = dpt[page_id]
            if record.lsn < rec_lsn:
                records_skipped += 1
                pages_skipped.add(page_id)
                continue

            # 4. Page boundary validation
            offset = record.offset
            after_image = record.after_image
            if offset is None or after_image is None or offset < 0 or offset + len(after_image) > PAGE_SIZE:
                raise RedoError(
                    f"Invalid {record.record_type.value} bounds at LSN {record.lsn}: offset={offset}, "
                    f"after_image_len={len(after_image) if after_image is not None else None}, "
                    f"page_size={PAGE_SIZE}"
                )


            # 5. Page-LSN test & Update Replay
            # Fetch the page into the buffer pool (increments pin count)
            page = self._buffer_manager.fetch_page(page_id)
            try:
                current_page_lsn = self._buffer_manager.get_page_lsn(page_id)

                if current_page_lsn >= record.lsn:
                    # Page already contains this update (or a later update) -> skip
                    self._buffer_manager.unpin_page(page_id, is_dirty=False)
                    records_skipped += 1
                    pages_skipped.add(page_id)
                else:
                    # Apply update
                    page.write(offset, after_image)
                    self._buffer_manager.set_page_lsn(page_id, record.lsn)
                    self._buffer_manager.unpin_page(page_id, is_dirty=True)
                    # Ensure page persistence
                    self._buffer_manager.flush_page(page_id)
                    records_redone += 1
                    pages_modified.add(page_id)
            except Exception:
                try:
                    self._buffer_manager.unpin_page(page_id, is_dirty=False)
                except Exception:
                    pass
                raise

        return RedoResult(
            redo_start_lsn=redo_start_lsn,
            last_scanned_lsn=last_scanned_lsn,
            records_scanned=records_scanned,
            records_redone=records_redone,
            records_skipped=records_skipped,
            pages_modified=sorted(list(pages_modified)),
            pages_skipped=sorted(list(pages_skipped)),
        )

    def run(self, analysis_result: AnalysisResult) -> RedoResult:
        """Alias for execute()."""
        return self.execute(analysis_result)

    @classmethod
    def recover(
        cls,
        log_manager: LogManager,
        buffer_manager: BufferManager,
        analysis_result: AnalysisResult,
    ) -> RedoResult:
        """Classmethod convenience entry point for executing Redo."""
        instance = cls(log_manager=log_manager, buffer_manager=buffer_manager)
        return instance.execute(analysis_result)

    @classmethod
    def redo(
        cls,
        log_manager: LogManager,
        buffer_manager: BufferManager,
        analysis_result: AnalysisResult,
    ) -> RedoResult:
        """Alias classmethod convenience entry point for executing Redo."""
        return cls.recover(log_manager=log_manager, buffer_manager=buffer_manager, analysis_result=analysis_result)
