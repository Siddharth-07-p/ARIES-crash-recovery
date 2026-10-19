# Phase 9: ARIES Undo and Compensation Log Records (CLRs) — Implementation Plan

## 1. Architectural Context & Objective

The objective of Phase 9 is implementing the **ARIES Undo Recovery Phase** and **Compensation Log Records (CLRs)** for the database crash recovery system. Building upon:
- Phase 7: Analysis Phase (reconstructing Transaction Table, DPT, and identifying winners/losers)
- Phase 8: Redo Phase (Repeating history for both winners and losers to bring the buffer/disk up to crash state)

Phase 9 completes the rollback of uncommitted loser transactions and transactions whose abort/rollback was interrupted by a crash.

---

## 2. CLR Log Record Format and Binary Serialization

A Compensation Log Record (CLR) represents the action taken to compensate (undo) an `UPDATE` log record. In ARIES, CLRs are **never undone**; they serve to bound the amount of work required during subsequent recoveries should another crash occur.

### Binary Layout (deterministic, network byte order `>`):
1. **Magic Header (4 bytes)**: `b"WALR"`
2. **Total Record Length (4 bytes)**: `uint32`
3. **LSN (8 bytes)**: `uint64` (strictly monotonic, positive)
4. **Prev LSN (8 bytes)**: `uint64` (0 if None) — points to the current transaction's previous log tail
5. **Transaction ID (8 bytes)**: `uint64` (positive integer)
6. **Record Type (1 byte)**: `uint8 = 7` (`TYPE_CODE_CLR`)
7. **Payload (20 + N bytes)**:
   - `page_id`: `uint32` (4 bytes)
   - `offset`: `uint32` (4 bytes)
   - `undo_next_lsn`: `uint64` (8 bytes, 0 if None) — points to the next earlier log record of the transaction that remains to be undone
   - `compensation_len`: `uint32` (4 bytes)
   - `compensation_image`: `bytes` (N bytes) — the before-image of the original update, used to redo the undo operation during restart Redo
8. **CRC32 Checksum (4 bytes)**: `uint32` computed across header and payload

### Strict Invariants & Validations:
- `page_id` is non-negative and valid.
- `offset >= 0` and `offset + len(compensation_image) <= PAGE_SIZE`.
- `undo_next_lsn` must be strictly less than the record's own LSN (`undo_next_lsn < lsn`). Forward or self references are prohibited.
- `undo_next_lsn` is strictly forbidden on non-CLR record types.

---

## 3. Undo Engine Algorithm

The Undo phase processes loser transactions to safely restore data pages to their pre-transaction states.

### Priority Queue Processing (Highest-LSN-First):
1. Reconstruct the loser transaction set:
   - All transactions marked as `RUNNING` in the Transaction Table.
   - Any transaction marked as `ABORTED` whose physical rollback was incomplete (has uncompensated `UPDATE` records).
2. Populate a max-heap (implemented as min-heap with negative LSNs: `(-last_lsn, tx_id)`):
   - For losers with valid log tails, push `(-last_lsn, tx_id)`.
   - For losers with no log records, immediately append a durable `ABORT` record and mark complete.
3. While heap is non-empty:
   - Pop `(neg_lsn, tx_id)`; `curr_lsn = -neg_lsn`.
   - Retrieve log record at `curr_lsn` from `LogManager`.
   - **Case `UPDATE`**:
     1. Validate bounds: `0 <= offset` and `offset + len(before_image) <= PAGE_SIZE`.
     2. Fetch page from `BufferManager`.
     3. Restore `before_image` at `offset`.
     4. Append `CLR` record with:
        - `prev_lsn` = transaction's current log tail.
        - `undo_next_lsn` = `record.prev_lsn`.
        - `compensation_image` = `record.before_image`.
     5. Flush WAL to disk (durable before page flush).
     6. Set `page_lsn` to the CLR LSN and mark page dirty.
     7. Unpin page and flush to disk.
     8. If `undo_next_lsn` is not None, push `(-undo_next_lsn, tx_id)` to heap; otherwise append durable `ABORT` completion record.
   - **Case `CLR`**:
     - Encountered during restart after partial Undo.
     - Skip re-undoing; follow `record.undo_next_lsn`.
     - If `undo_next_lsn` is None, append durable `ABORT` completion record; otherwise push `(-undo_next_lsn, tx_id)` to heap.
   - **Case `BEGIN`**:
     - Reached transaction start; append durable `ABORT` completion record and mark transaction completed.
   - **Case `ABORT`**:
     - Skip record; follow `record.prev_lsn` to undo prior updates if rollback was incomplete.
   - **Case `COMMIT`**:
     - Committed winner; never undone.

---

## 4. Aborted Transactions and Completion Handling

In ARIES, an `ABORT` record in the log indicates the decision or initiation of an abort, but does **not** prove that physical rollback finished.
- An aborted transaction whose rollback was interrupted by a crash still contains dirty updates on disk/pages.
- **Rule**: If backward traversal from `tx.last_lsn` encounters an `UPDATE` record (or a CLR whose `undo_next_lsn` points to an `UPDATE`), physical rollback was incomplete.
- Undo adds incomplete aborted transactions to the rollback queue, undoes remaining updates, and appends a durable completion `ABORT` record.
- Once all updates are compensated by CLRs and terminated with a completion record, subsequent recovery recognizes the transaction as completed and never undoes it again.

---

## 5. Page Restoration & WAL Durability Invariant

To satisfy Write-Ahead Logging requirements:
1. Dirty page data restored in memory cannot reach disk before the corresponding CLR log record is written and fsynced.
2. In `Undo._execute_internal`, `self._log_manager.flush()` is invoked **before** `self._buffer_manager.flush_page(page_id)`.
3. In `BufferManager.flush_page()`, `_ensure_wal_flushed_for_frame()` verifies `lm.flushed_lsn >= page_lsn`.
4. Page pin counts are protected with `try...finally` to ensure frames are never left pinned if validation or fault injection crashes occur.

---

## 6. Restart Safety & `undo_next_lsn` Mechanics

When a crash occurs during Undo:
- Any CLR that reached disk is permanent.
- On subsequent restart:
  1. Analysis reconstructs the transaction table and DPT.
  2. Redo replays CLRs along with UPDATEs ("repeating history").
  3. Undo reads the CLR at the transaction's tail and follows its `undo_next_lsn`.
  4. The already-compensated update is never undone a second time.
  5. Consecutive crashes during Undo are fully supported and monotonic.

---

## 7. Fault Injection Points

Three fault injection points are supported:
- `DURING_UNDO_BEFORE_CLR_FLUSH`: Triggered prior to making CLR durable.
- `DURING_UNDO_AFTER_CLR_FLUSH_BEFORE_PAGE_FLUSH`: Triggered after CLR is durable in WAL but before restored page is persisted.
- `DURING_UNDO_AFTER_PAGE_FLUSH`: Triggered after the restored page is persisted to disk.

Subprocess tests execute real process terminations (`sys.exit(42)`) to verify that the OS process dies and persistent files can be safely recovered by a new process.

---

## 8. UndoResult Immutability and Error Handling

`UndoResult` encapsulates:
- `loser_transactions`: Total losers identified
- `transactions_undone`: Total transactions rolled back
- `clrs_generated`: Count of CLR records written
- `updates_undone`: Count of UPDATE records undone
- `pages_modified`: Sorted list of unique modified page IDs
- `records_skipped`: Non-update records processed
- `last_processed_lsn`: Highest LSN processed
- `completed_transactions`: Sorted list of completed transaction IDs
- `remaining_losers`: Sorted list of incomplete transactions (empty on completion)
- `is_complete`: Boolean flag indicating full recovery completion
- Immutable slots and defensively copied lists.
