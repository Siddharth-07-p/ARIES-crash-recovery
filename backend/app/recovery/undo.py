"""ARIES Undo Phase implementation for database crash recovery."""

import heapq
from typing import Any, List, Optional, Set, Tuple, Union
from app.buffer.buffer_manager import BufferManager
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector
from app.recovery.exceptions import UndoError
from app.recovery.recovery_state import AnalysisResult, UndoResult
from app.storage.constants import PAGE_SIZE
from app.transaction.transaction import TransactionState
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecordType


class Undo:
    """Executes the ARIES Undo recovery phase.

    Undo rolls back active uncommitted (loser) transactions identified by Analysis:
        1. Selects the active loser transaction with the largest LSN (global priority queue).
        2. Follows transaction prev_lsn chains backward.
        3. Undoes UPDATE records by restoring their before_image.
        4. Appends a Compensation Log Record (CLR) with undo_next_lsn pointing to record.prev_lsn.
        5. Flushes WAL before writing modified page to disk (WAL-before-page-write invariant).
        6. Updates page_lsn to CLR LSN and persists page.
        7. If encountering a CLR (from previous interrupted recovery), follows record.undo_next_lsn.
        8. Once all updates for a transaction are undone, appends an ABORT completion record.
    """

    def __init__(
        self,
        log_manager: LogManager,
        buffer_manager: BufferManager,
    ) -> None:
        """Initialize Undo with a LogManager and BufferManager.

        Args:
            log_manager: Persistent LogManager instance for WAL reading and appending CLRs/ABORTs.
            buffer_manager: BufferManager instance for fetching and persisting page modifications.

        Raises:
            UndoError: If log_manager or buffer_manager is None.
        """
        if log_manager is None:
            raise UndoError("log_manager cannot be None")
        if buffer_manager is None:
            raise UndoError("buffer_manager cannot be None")

        self._log_manager: LogManager = log_manager
        self._buffer_manager: BufferManager = buffer_manager

    def execute(
        self_or_analysis: Any,
        analysis_result_or_lm: Any = None,
        buffer_manager: Optional[BufferManager] = None,
    ) -> UndoResult:
        """Execute the ARIES Undo phase.

        Supports both:
            - Instance method: undo_instance.execute(analysis_result)
            - Class method: Undo.execute(analysis_result, log_manager, buffer_manager)

        Args:
            self_or_analysis: Undo instance (if instance method) or AnalysisResult (if classmethod).
            analysis_result_or_lm: AnalysisResult (if instance method) or LogManager (if classmethod).
            buffer_manager: BufferManager instance (if classmethod).

        Returns:
            UndoResult summarizing loser transactions rolled back and CLRs written.

        Raises:
            UndoError: If parameters are invalid or log/page bounds are violated.
        """
        if isinstance(self_or_analysis, Undo):
            analysis_result = analysis_result_or_lm
            return self_or_analysis._execute_internal(analysis_result)
        else:
            analysis_result = self_or_analysis
            log_manager = analysis_result_or_lm
            instance = Undo(log_manager=log_manager, buffer_manager=buffer_manager)
            return instance._execute_internal(analysis_result)

    def _is_transaction_rollback_incomplete(self, tx_id: int, last_lsn: Optional[int]) -> bool:
        """Determine if an aborted transaction has uncompensated updates requiring Undo.

        In ARIES, an ABORT log record indicates the decision/start of abort, but does
        not prove that physical rollback finished. A transaction's rollback is complete
        only if:
            1. It never executed any UPDATE records, OR
            2. All of its UPDATE records have already been compensated by CLR records,
               culminating in a final completion/abort record.

        We trace the transaction log chain backwards:
            - If we encounter an UPDATE record: that update was never undone -> incomplete.
            - If we encounter a CLR: follow its undo_next_lsn.
              If undo_next_lsn is None or points to a BEGIN record: all updates undone -> complete.
              If undo_next_lsn points to an UPDATE: that update still needs undo -> incomplete.
            - If we encounter an ABORT record: follow its prev_lsn.
            - If we reach BEGIN or None without uncompensated UPDATEs: -> complete.
        """
        curr_lsn = last_lsn
        visited: Set[int] = set()
        while curr_lsn is not None:
            if curr_lsn in visited:
                break
            visited.add(curr_lsn)
            rec = self._log_manager.get_log_record(curr_lsn)
            if rec is None:
                return True
            if rec.record_type == LogRecordType.UPDATE:
                return True
            elif rec.record_type == LogRecordType.CLR:
                if rec.undo_next_lsn is None:
                    return False
                if rec.undo_next_lsn >= curr_lsn:
                    return True
                curr_lsn = rec.undo_next_lsn
            elif rec.record_type == LogRecordType.BEGIN:
                return False
            else:
                if rec.prev_lsn is not None and rec.prev_lsn >= curr_lsn:
                    return True
                curr_lsn = rec.prev_lsn
        return False

    def _validate_transaction_chain(self, tx_id: int, start_lsn: Optional[int]) -> None:
        """Validate that the transaction log chain is acyclic and strictly monotonic before mutating state."""
        curr_lsn = start_lsn
        visited: Set[int] = set()

        while curr_lsn is not None:
            if curr_lsn in visited:
                raise UndoError(
                    f"Detected cyclic log chain for transaction {tx_id} at LSN {curr_lsn}"
                )
            visited.add(curr_lsn)

            rec = self._log_manager.get_log_record(curr_lsn)
            if rec is None:
                raise UndoError(
                    f"Log record at LSN {curr_lsn} for transaction {tx_id} not found in WAL"
                )

            if rec.transaction_id is not None and rec.transaction_id != tx_id:
                raise UndoError(
                    f"Log record at LSN {curr_lsn} belongs to transaction {rec.transaction_id}, expected {tx_id}"
                )

            if rec.record_type == LogRecordType.UPDATE:
                next_lsn = rec.prev_lsn
            elif rec.record_type == LogRecordType.CLR:
                next_lsn = rec.undo_next_lsn
            else:
                next_lsn = rec.prev_lsn

            if next_lsn is not None:
                if next_lsn >= curr_lsn or next_lsn >= self._log_manager.next_lsn:
                    raise UndoError(
                        f"Detected invalid forward-pointing log chain for transaction {tx_id}: "
                        f"reference {next_lsn} >= current LSN {curr_lsn}"
                    )
            curr_lsn = next_lsn

    def _execute_internal(self, analysis_result: AnalysisResult) -> UndoResult:
        """Internal execution of the ARIES Undo phase on the reconstructed analysis state.

        Args:
            analysis_result: Reconstructed recovery state from the Analysis phase.

        Returns:
            UndoResult summarizing loser transactions rolled back and CLRs written.

        Raises:
            UndoError: If analysis_result is invalid, log records are missing/corrupt,
                       or page bounds are violated.
        """
        if analysis_result is None or not isinstance(analysis_result, AnalysisResult):
            raise UndoError("analysis_result must be a valid AnalysisResult instance")

        tx_table = analysis_result.transaction_table

        # Identify all transactions requiring rollback:
        # 1. Active loser transactions from Analysis (RUNNING state)
        # 2. Incomplete aborted transactions (ABORTED state with uncompensated updates)
        loser_set: Set[int] = set(analysis_result.losers)
        for tx_id, tx in tx_table.items():
            if tx.state == TransactionState.ABORTED:
                if self._is_transaction_rollback_incomplete(tx_id, tx.last_lsn):
                    loser_set.add(tx_id)

        losers = sorted(list(loser_set))
        total_losers = len(losers)

        if total_losers == 0:
            return UndoResult(
                loser_transactions=0,
                transactions_undone=0,
                clrs_generated=0,
                updates_undone=0,
                pages_modified=[],
                records_skipped=0,
                last_processed_lsn=None,
                completed_transactions=[],
                remaining_losers=[],
                is_complete=True,
            )

        # Pre-validate log chains for all loser transactions to ensure acyclic and monotonic references
        for tx_id in losers:
            if tx_id in tx_table and tx_table[tx_id].last_lsn is not None:
                self._validate_transaction_chain(tx_id, tx_table[tx_id].last_lsn)

        # Priority queue of (-current_lsn, tx_id) to always process highest LSN across losers first
        # Format in heap: (-lsn, tx_id)
        heap: List[Tuple[int, int]] = []
        completed_txs: Set[int] = set()

        for tx_id in losers:
            if tx_id in tx_table and tx_table[tx_id].last_lsn is not None:
                last_lsn = tx_table[tx_id].last_lsn
                assert last_lsn is not None
                heapq.heappush(heap, (-last_lsn, tx_id))
            else:
                # Loser transaction has no log records: complete immediately with ABORT record
                self._log_manager.append_abort(tx_id)
                self._log_manager.flush()
                completed_txs.add(tx_id)

        clrs_generated = 0
        updates_undone = 0
        records_skipped = 0
        pages_modified: Set[int] = set()
        last_processed_lsn: Optional[int] = None
        visited_steps: Set[Tuple[int, int]] = set()

        while heap:
            neg_lsn, tx_id = heapq.heappop(heap)
            curr_lsn = -neg_lsn
            last_processed_lsn = curr_lsn

            # Guard against invalid cyclic chains in corrupted WAL
            step_key = (tx_id, curr_lsn)
            if step_key in visited_steps:
                raise UndoError(
                    f"Detected cyclic log chain for transaction {tx_id} at LSN {curr_lsn}"
                )
            visited_steps.add(step_key)

            record = self._log_manager.get_log_record(curr_lsn)
            if record is None:
                raise UndoError(f"Log record at LSN {curr_lsn} for transaction {tx_id} not found in WAL")

            if record.record_type == LogRecordType.UPDATE:
                page_id = record.page_id
                offset = record.offset
                before_image = record.before_image

                if page_id is None or offset is None or before_image is None:
                    raise UndoError(f"Malformed UPDATE record at LSN {curr_lsn}: missing page/offset/before_image")

                if offset < 0 or offset + len(before_image) > PAGE_SIZE:
                    raise UndoError(
                        f"Invalid UPDATE bounds during Undo at LSN {curr_lsn}: offset={offset}, "
                        f"len={len(before_image)}, page_size={PAGE_SIZE}"
                    )

                undo_next_lsn = record.prev_lsn
                if undo_next_lsn is not None and (
                    undo_next_lsn >= curr_lsn or undo_next_lsn >= self._log_manager.next_lsn
                ):
                    raise UndoError(
                        f"Detected invalid forward-pointing log chain for transaction {tx_id}: "
                        f"prev_lsn {undo_next_lsn} >= current LSN {curr_lsn}"
                    )

                # 1. Append CLR to WAL and ensure it is durable
                fault_injector.trigger(FaultPoint.DURING_UNDO_BEFORE_CLR_FLUSH)
                try:
                    clr_lsn = self._log_manager.append_clr(
                        transaction_id=tx_id,
                        page_id=page_id,
                        offset=offset,
                        undo_next_lsn=undo_next_lsn,
                        compensation_image=before_image,
                    )
                except Exception as exc:
                    raise UndoError(f"Failed to append CLR for transaction {tx_id}: {exc}") from exc
                self._log_manager.flush()
                fault_injector.trigger(FaultPoint.DURING_UNDO_AFTER_CLR_FLUSH_BEFORE_PAGE_FLUSH)

                # 2. Fetch page, apply compensation before_image, update page_lsn and flush page safely
                page = None
                try:
                    page = self._buffer_manager.fetch_page(page_id)
                    page.write(offset, before_image)
                    self._buffer_manager.set_page_lsn(page_id, clr_lsn)
                    self._buffer_manager.unpin_page(page_id, is_dirty=True)
                    page = None  # Successfully unpinned

                    self._buffer_manager.flush_page(page_id)
                    fault_injector.trigger(FaultPoint.DURING_UNDO_AFTER_PAGE_FLUSH)
                finally:
                    if page is not None:
                        try:
                            self._buffer_manager.unpin_page(page_id, is_dirty=False)
                        except Exception:
                            pass

                clrs_generated += 1
                updates_undone += 1
                pages_modified.add(page_id)

                # 4. Advance transaction pointer
                if undo_next_lsn is not None:
                    heapq.heappush(heap, (-undo_next_lsn, tx_id))
                else:
                    # No more updates to undo for this transaction: write ABORT record
                    self._log_manager.append_abort(tx_id)
                    self._log_manager.flush()
                    completed_txs.add(tx_id)

            elif record.record_type == LogRecordType.CLR:
                # Interrupted recovery encountered an existing CLR: skip and jump to undo_next_lsn
                records_skipped += 1
                undo_next = record.undo_next_lsn
                if undo_next is not None:
                    if undo_next >= curr_lsn:
                        raise UndoError(
                            f"Detected invalid forward-pointing undo_next_lsn for transaction {tx_id}: "
                            f"{undo_next} >= current LSN {curr_lsn}"
                        )
                    heapq.heappush(heap, (-undo_next, tx_id))
                else:
                    # CLR was the final undo step: append ABORT record
                    self._log_manager.append_abort(tx_id)
                    self._log_manager.flush()
                    completed_txs.add(tx_id)

            elif record.record_type == LogRecordType.BEGIN:
                # Reached BEGIN record: transaction is fully undone
                records_skipped += 1
                prev_lsn = record.prev_lsn
                if prev_lsn is not None:
                    if prev_lsn >= curr_lsn:
                        raise UndoError(
                            f"Detected invalid forward-pointing log chain for transaction {tx_id}: "
                            f"prev_lsn {prev_lsn} >= current LSN {curr_lsn}"
                        )
                    heapq.heappush(heap, (-prev_lsn, tx_id))
                else:
                    self._log_manager.append_abort(tx_id)
                    self._log_manager.flush()
                    completed_txs.add(tx_id)

            elif record.record_type == LogRecordType.ABORT:
                # Encountered ABORT record during backward traversal
                records_skipped += 1
                if record.prev_lsn is not None:
                    if record.prev_lsn >= curr_lsn:
                        raise UndoError(
                            f"Detected invalid forward-pointing log chain for transaction {tx_id}: "
                            f"prev_lsn {record.prev_lsn} >= current LSN {curr_lsn}"
                        )
                    heapq.heappush(heap, (-record.prev_lsn, tx_id))
                else:
                    completed_txs.add(tx_id)

            elif record.record_type == LogRecordType.COMMIT:
                # Committed winner transaction - should never be undone
                records_skipped += 1
                completed_txs.add(tx_id)

            else:
                # Non-transaction or checkpoint record
                records_skipped += 1
                if record.prev_lsn is not None:
                    if record.prev_lsn >= curr_lsn:
                        raise UndoError(
                            f"Detected invalid forward-pointing log chain for transaction {tx_id}: "
                            f"prev_lsn {record.prev_lsn} >= current LSN {curr_lsn}"
                        )
                    heapq.heappush(heap, (-record.prev_lsn, tx_id))
                else:
                    self._log_manager.append_abort(tx_id)
                    self._log_manager.flush()
                    completed_txs.add(tx_id)

        remaining = [t for t in losers if t not in completed_txs]

        return UndoResult(
            loser_transactions=total_losers,
            transactions_undone=len(completed_txs),
            clrs_generated=clrs_generated,
            updates_undone=updates_undone,
            pages_modified=sorted(list(pages_modified)),
            records_skipped=records_skipped,
            last_processed_lsn=last_processed_lsn,
            completed_transactions=sorted(list(completed_txs)),
            remaining_losers=sorted(remaining),
            is_complete=len(remaining) == 0,
        )

    def run(self, analysis_result: AnalysisResult) -> UndoResult:
        """Alias for execute()."""
        return self.execute(analysis_result)

    @classmethod
    def recover(
        cls,
        log_manager: LogManager,
        buffer_manager: BufferManager,
        analysis_result: AnalysisResult,
    ) -> UndoResult:
        """Classmethod convenience entry point for executing Undo."""
        instance = cls(log_manager=log_manager, buffer_manager=buffer_manager)
        return instance.execute(analysis_result)

    @classmethod
    def undo(
        cls,
        log_manager: LogManager,
        buffer_manager: BufferManager,
        analysis_result: AnalysisResult,
    ) -> UndoResult:
        """Alias classmethod convenience entry point for executing Undo."""
        return cls.recover(log_manager=log_manager, buffer_manager=buffer_manager, analysis_result=analysis_result)
