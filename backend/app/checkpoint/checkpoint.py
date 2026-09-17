"""Checkpoint snapshot structures, deterministic binary serialization, and metadata."""

import struct
from typing import Any, Dict, NamedTuple, Optional, Tuple, Union
from app.checkpoint.exceptions import CheckpointCorruptionError, InvalidCheckpointError
from app.transaction.transaction import TransactionState

SNAPSHOT_MAGIC = b"CPSS"
SNAPSHOT_VERSION = 1

_STATE_TO_CODE = {
    TransactionState.RUNNING: 1,
    TransactionState.COMMITTED: 2,
    TransactionState.ABORTED: 3,
}

_CODE_TO_STATE = {
    1: TransactionState.RUNNING,
    2: TransactionState.COMMITTED,
    3: TransactionState.ABORTED,
}


class TransactionTableEntry:
    """Represents a transaction entry captured during checkpointing.

    Attributes:
        transaction_id: Unique positive transaction identifier.
        state: TransactionState (RUNNING, COMMITTED, ABORTED).
        last_lsn: Most recent LSN written by this transaction (or None).
    """

    __slots__ = ("_transaction_id", "_state", "_last_lsn")

    def __init__(
        self,
        transaction_id: int,
        state: Union[TransactionState, str],
        last_lsn: Optional[int] = None,
    ) -> None:
        """Initialize TransactionTableEntry."""
        if not isinstance(transaction_id, int) or isinstance(transaction_id, bool) or transaction_id <= 0:
            raise InvalidCheckpointError(f"transaction_id must be a positive integer, got {transaction_id!r}")

        if isinstance(state, str):
            try:
                state = TransactionState(state)
            except ValueError:
                raise InvalidCheckpointError(f"Invalid transaction state: {state!r}")

        if not isinstance(state, TransactionState):
            raise InvalidCheckpointError(f"Invalid transaction state type: {type(state).__name__}")

        if last_lsn is not None:
            if not isinstance(last_lsn, int) or isinstance(last_lsn, bool) or last_lsn <= 0:
                raise InvalidCheckpointError(f"last_lsn must be a positive integer or None, got {last_lsn!r}")

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

    @property
    def status(self) -> TransactionState:
        """Alias for state."""
        return self._state

    @property
    def last_lsn(self) -> Optional[int]:
        """Return the last recorded LSN for the transaction."""
        return self._last_lsn

    def to_dict(self) -> Dict[str, Any]:
        """Convert entry to dictionary."""
        return {
            "transaction_id": self._transaction_id,
            "state": self._state.value,
            "last_lsn": self._last_lsn,
        }

    def __eq__(self, other: object) -> bool:
        """Equality comparison."""
        if not isinstance(other, TransactionTableEntry):
            return False
        return (
            self._transaction_id == other._transaction_id
            and self._state == other._state
            and self._last_lsn == other._last_lsn
        )

    def __repr__(self) -> str:
        """String representation."""
        return f"TransactionTableEntry(id={self._transaction_id}, state={self._state.value}, last_lsn={self._last_lsn})"


class CheckpointSnapshot:
    """Immutable, deterministic snapshot of Transaction Table and Dirty Page Table."""

    __slots__ = ("_transaction_table", "_dirty_page_table")

    def __init__(
        self,
        transaction_table: Optional[Dict[int, Union[TransactionTableEntry, Dict[str, Any]]]] = None,
        dirty_page_table: Optional[Dict[int, int]] = None,
    ) -> None:
        """Initialize a CheckpointSnapshot with transaction table and DPT mappings."""
        tx_table: Dict[int, TransactionTableEntry] = {}
        if transaction_table:
            for tx_id, entry in transaction_table.items():
                if isinstance(entry, TransactionTableEntry):
                    tx_table[tx_id] = TransactionTableEntry(
                        transaction_id=entry.transaction_id,
                        state=entry.state,
                        last_lsn=entry.last_lsn,
                    )
                elif isinstance(entry, dict):
                    tx_table[tx_id] = TransactionTableEntry(
                        transaction_id=entry["transaction_id"],
                        state=entry["state"],
                        last_lsn=entry.get("last_lsn"),
                    )
                else:
                    raise InvalidCheckpointError(
                        f"Unsupported transaction table entry format: {type(entry).__name__}"
                    )

        dpt: Dict[int, int] = {}
        if dirty_page_table:
            for page_id, rec_lsn in dirty_page_table.items():
                if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
                    raise InvalidCheckpointError(f"Invalid page_id in DPT: {page_id!r}")
                if not isinstance(rec_lsn, int) or isinstance(rec_lsn, bool) or rec_lsn <= 0:
                    raise InvalidCheckpointError(f"Invalid recLSN in DPT: {rec_lsn!r}")
                dpt[page_id] = rec_lsn

        self._transaction_table: Dict[int, TransactionTableEntry] = tx_table
        self._dirty_page_table: Dict[int, int] = dpt

    @property
    def transaction_table(self) -> Dict[int, TransactionTableEntry]:
        """Return a copy of the transaction table snapshot."""
        return dict(self._transaction_table)

    @property
    def dirty_page_table(self) -> Dict[int, int]:
        """Return a copy of the Dirty Page Table snapshot."""
        return dict(self._dirty_page_table)

    def serialize(self) -> bytes:
        """Serialize snapshot to deterministic binary format without pickle.

        Binary Format:
            - Magic: b'CPSS' (4 bytes)
            - Version: uint16 (2 bytes, 1)
            - Num Transactions: uint32 (4 bytes)
            - For each transaction (sorted by tx_id):
                - tx_id: int64 (8 bytes)
                - state_code: uint8 (1 byte: 1=RUNNING, 2=COMMITTED, 3=ABORTED)
                - last_lsn: uint64 (8 bytes, 0 if None)
            - Num Dirty Pages: uint32 (4 bytes)
            - For each dirty page (sorted by page_id):
                - page_id: uint32 (4 bytes)
                - rec_lsn: uint64 (8 bytes)
        """
        buf = bytearray()
        buf += SNAPSHOT_MAGIC
        buf += struct.pack(">HI", SNAPSHOT_VERSION, len(self._transaction_table))

        for tx_id in sorted(self._transaction_table.keys()):
            entry = self._transaction_table[tx_id]
            state_code = _STATE_TO_CODE[entry.state]
            last_lsn_val = entry.last_lsn if entry.last_lsn is not None else 0
            buf += struct.pack(">qBQ", entry.transaction_id, state_code, last_lsn_val)

        buf += struct.pack(">I", len(self._dirty_page_table))
        for page_id in sorted(self._dirty_page_table.keys()):
            rec_lsn = self._dirty_page_table[page_id]
            buf += struct.pack(">IQ", page_id, rec_lsn)

        return bytes(buf)

    @classmethod
    def deserialize(cls, data: bytes) -> "CheckpointSnapshot":
        """Deserialize a CheckpointSnapshot from binary data.

        Raises:
            CheckpointCorruptionError: If data is truncated, malformed, or corrupt.
        """
        if len(data) < 10:  # 4 magic + 2 version + 4 num_tx
            raise CheckpointCorruptionError(
                f"Truncated checkpoint snapshot: expected at least 10 bytes, got {len(data)}"
            )

        magic = data[:4]
        if magic != SNAPSHOT_MAGIC:
            raise CheckpointCorruptionError(
                f"Invalid checkpoint snapshot magic: expected {SNAPSHOT_MAGIC!r}, got {magic!r}"
            )

        version, num_tx = struct.unpack_from(">HI", data, 4)
        if version != SNAPSHOT_VERSION:
            raise CheckpointCorruptionError(f"Unsupported snapshot version: {version}")

        offset = 10
        tx_table: Dict[int, TransactionTableEntry] = {}
        for _ in range(num_tx):
            if len(data) - offset < 17:  # 8 (tx_id) + 1 (state) + 8 (last_lsn)
                raise CheckpointCorruptionError("Truncated transaction table in checkpoint snapshot")

            tx_id, state_code, last_lsn_val = struct.unpack_from(">qBQ", data, offset)
            offset += 17

            if state_code not in _CODE_TO_STATE:
                raise CheckpointCorruptionError(f"Unknown transaction state code: {state_code}")

            state = _CODE_TO_STATE[state_code]
            last_lsn = last_lsn_val if last_lsn_val > 0 else None
            tx_table[tx_id] = TransactionTableEntry(
                transaction_id=tx_id,
                state=state,
                last_lsn=last_lsn,
            )

        if len(data) - offset < 4:
            raise CheckpointCorruptionError("Truncated dirty page table count in checkpoint snapshot")

        num_dpt, = struct.unpack_from(">I", data, offset)
        offset += 4

        dpt: Dict[int, int] = {}
        for _ in range(num_dpt):
            if len(data) - offset < 12:  # 4 (page_id) + 8 (rec_lsn)
                raise CheckpointCorruptionError("Truncated dirty page table entry in checkpoint snapshot")

            page_id, rec_lsn = struct.unpack_from(">IQ", data, offset)
            offset += 12
            dpt[page_id] = rec_lsn

        if offset != len(data):
            raise CheckpointCorruptionError(
                f"Extraneous bytes in checkpoint snapshot: expected {offset} bytes, found {len(data)}"
            )

        return cls(transaction_table=tx_table, dirty_page_table=dpt)

    def to_dict(self) -> Dict[str, Any]:
        """Convert snapshot to dictionary representation."""
        return {
            "transaction_table": {
                tx_id: self._transaction_table[tx_id].to_dict()
                for tx_id in sorted(self._transaction_table.keys())
            },
            "dirty_page_table": {
                page_id: self._dirty_page_table[page_id]
                for page_id in sorted(self._dirty_page_table.keys())
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckpointSnapshot":
        """Reconstruct CheckpointSnapshot from dictionary representation."""
        tx_table_data = data.get("transaction_table", {})
        dpt_data = data.get("dirty_page_table", {})

        tx_table: Dict[int, TransactionTableEntry] = {}
        for k, v in tx_table_data.items():
            tx_id = int(k)
            tx_table[tx_id] = TransactionTableEntry(
                transaction_id=v["transaction_id"],
                state=v["state"],
                last_lsn=v.get("last_lsn"),
            )

        dpt: Dict[int, int] = {int(k): int(v) for k, v in dpt_data.items()}
        return cls(transaction_table=tx_table, dirty_page_table=dpt)

    def __eq__(self, other: object) -> bool:
        """Equality check."""
        if not isinstance(other, CheckpointSnapshot):
            return False
        return (
            self._transaction_table == other._transaction_table
            and self._dirty_page_table == other._dirty_page_table
        )

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"CheckpointSnapshot(transactions={len(self._transaction_table)}, "
            f"dirty_pages={len(self._dirty_page_table)})"
        )


class CheckpointInfo:
    """Metadata describing a completed checkpoint.

    Attributes:
        begin_lsn: LSN of the corresponding CHECKPOINT_BEGIN log record.
        end_lsn: LSN of the corresponding CHECKPOINT_END log record.
        snapshot: Deserialized CheckpointSnapshot.
    """

    __slots__ = ("_begin_lsn", "_end_lsn", "_snapshot")

    def __init__(
        self,
        begin_lsn: int,
        end_lsn: int,
        snapshot: CheckpointSnapshot,
    ) -> None:
        """Initialize CheckpointInfo."""
        if not isinstance(begin_lsn, int) or isinstance(begin_lsn, bool) or begin_lsn <= 0:
            raise InvalidCheckpointError(f"begin_lsn must be a positive integer, got {begin_lsn!r}")
        if not isinstance(end_lsn, int) or isinstance(end_lsn, bool) or end_lsn <= 0:
            raise InvalidCheckpointError(f"end_lsn must be a positive integer, got {end_lsn!r}")
        if end_lsn <= begin_lsn:
            raise InvalidCheckpointError(
                f"end_lsn ({end_lsn}) must be strictly greater than begin_lsn ({begin_lsn})"
            )
        if not isinstance(snapshot, CheckpointSnapshot):
            raise InvalidCheckpointError(f"snapshot must be CheckpointSnapshot, got {type(snapshot).__name__}")

        self._begin_lsn: int = begin_lsn
        self._end_lsn: int = end_lsn
        self._snapshot: CheckpointSnapshot = snapshot

    @property
    def begin_lsn(self) -> int:
        """Return the CHECKPOINT_BEGIN LSN."""
        return self._begin_lsn

    @property
    def end_lsn(self) -> int:
        """Return the CHECKPOINT_END LSN."""
        return self._end_lsn

    @property
    def snapshot(self) -> CheckpointSnapshot:
        """Return the CheckpointSnapshot."""
        return self._snapshot

    def to_dict(self) -> Dict[str, Any]:
        """Convert metadata to dictionary."""
        return {
            "begin_lsn": self._begin_lsn,
            "end_lsn": self._end_lsn,
            "snapshot": self._snapshot.to_dict(),
        }

    def __eq__(self, other: object) -> bool:
        """Equality check."""
        if not isinstance(other, CheckpointInfo):
            return False
        return (
            self._begin_lsn == other._begin_lsn
            and self._end_lsn == other._end_lsn
            and self._snapshot == other._snapshot
        )

    def __repr__(self) -> str:
        """String representation."""
        return f"CheckpointInfo(begin_lsn={self._begin_lsn}, end_lsn={self._end_lsn}, snapshot={self._snapshot})"
