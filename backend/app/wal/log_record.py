"""Log record definition, types, and deterministic binary serialization."""

from enum import Enum
import struct
from typing import Any, Dict, Optional, Tuple, Union
import zlib

from app.storage.constants import PAGE_SIZE
from app.wal.constants import (
    MIN_RECORD_SIZE,
    TYPE_CODE_ABORT,
    TYPE_CODE_BEGIN,
    TYPE_CODE_CHECKPOINT_BEGIN,
    TYPE_CODE_CHECKPOINT_END,
    TYPE_CODE_CLR,
    TYPE_CODE_COMMIT,
    TYPE_CODE_UPDATE,
    WAL_RECORD_MAGIC,
)
from app.wal.exceptions import InvalidLogRecordError, LogCorruptionError


class LogRecordType(str, Enum):
    """Enumeration of WAL log record types."""

    BEGIN = "BEGIN"
    COMMIT = "COMMIT"
    ABORT = "ABORT"
    UPDATE = "UPDATE"
    CHECKPOINT_BEGIN = "CHECKPOINT_BEGIN"
    CHECKPOINT_END = "CHECKPOINT_END"
    CLR = "CLR"


_TYPE_TO_CODE = {
    LogRecordType.BEGIN: TYPE_CODE_BEGIN,
    LogRecordType.COMMIT: TYPE_CODE_COMMIT,
    LogRecordType.ABORT: TYPE_CODE_ABORT,
    LogRecordType.UPDATE: TYPE_CODE_UPDATE,
    LogRecordType.CHECKPOINT_BEGIN: TYPE_CODE_CHECKPOINT_BEGIN,
    LogRecordType.CHECKPOINT_END: TYPE_CODE_CHECKPOINT_END,
    LogRecordType.CLR: TYPE_CODE_CLR,
}

_CODE_TO_TYPE = {
    TYPE_CODE_BEGIN: LogRecordType.BEGIN,
    TYPE_CODE_COMMIT: LogRecordType.COMMIT,
    TYPE_CODE_ABORT: LogRecordType.ABORT,
    TYPE_CODE_UPDATE: LogRecordType.UPDATE,
    TYPE_CODE_CHECKPOINT_BEGIN: LogRecordType.CHECKPOINT_BEGIN,
    TYPE_CODE_CHECKPOINT_END: LogRecordType.CHECKPOINT_END,
    TYPE_CODE_CLR: LogRecordType.CLR,
}


class LogRecord:
    """Represents a single deterministic Write-Ahead Log (WAL) record.

    Attributes:
        lsn: Unique Log Sequence Number (positive integer).
        record_type: Type of log entry (BEGIN, COMMIT, ABORT, UPDATE, CHECKPOINT_BEGIN, CHECKPOINT_END, CLR).
        transaction_id: Transaction identifier associated with this record.
        prev_lsn: LSN of the preceding record for the same transaction or begin LSN for checkpoint (or None).
        page_id: Target page ID for UPDATE and CLR records.
        offset: Byte offset within the page for UPDATE and CLR records.
        before_image: State of modified bytes before update (for UNDO).
        after_image: State of modified bytes after update (for REDO or CLR compensation).
        checkpoint_payload: Serialized payload data for CHECKPOINT_END records.
        undo_next_lsn: Next LSN to undo for this transaction (for CLR records).
    """

    __slots__ = (
        "_lsn",
        "_record_type",
        "_transaction_id",
        "_prev_lsn",
        "_page_id",
        "_offset",
        "_before_image",
        "_after_image",
        "_checkpoint_payload",
        "_undo_next_lsn",
    )

    def __init__(
        self,
        lsn: int,
        record_type: Union[LogRecordType, str],
        transaction_id: Optional[int] = None,
        prev_lsn: Optional[int] = None,
        page_id: Optional[int] = None,
        offset: Optional[int] = None,
        before_image: Optional[Union[bytes, bytearray, memoryview]] = None,
        after_image: Optional[Union[bytes, bytearray, memoryview]] = None,
        checkpoint_payload: Optional[Union[bytes, bytearray, memoryview]] = None,
        undo_next_lsn: Optional[int] = None,
    ) -> None:
        """Initialize and validate a LogRecord instance."""
        if not isinstance(lsn, int) or isinstance(lsn, bool) or lsn <= 0:
            raise InvalidLogRecordError(f"LSN must be a positive integer, got {lsn!r}")

        if isinstance(record_type, str):
            try:
                record_type = LogRecordType(record_type)
            except ValueError:
                raise InvalidLogRecordError(f"Invalid record type: {record_type!r}")

        if not isinstance(record_type, LogRecordType):
            raise InvalidLogRecordError(f"record_type must be LogRecordType, got {type(record_type).__name__}")

        if transaction_id is not None:
            if not isinstance(transaction_id, int) or isinstance(transaction_id, bool) or transaction_id <= 0:
                raise InvalidLogRecordError(
                    f"transaction_id must be a positive integer, got {transaction_id!r}"
                )

        if prev_lsn is not None:
            if not isinstance(prev_lsn, int) or isinstance(prev_lsn, bool) or prev_lsn < 0:
                raise InvalidLogRecordError(
                    f"prev_lsn must be a non-negative integer or None, got {prev_lsn!r}"
                )
            if prev_lsn == 0:
                prev_lsn = None

        if undo_next_lsn is not None:
            if not isinstance(undo_next_lsn, int) or isinstance(undo_next_lsn, bool) or undo_next_lsn <= 0:
                raise InvalidLogRecordError(
                    f"undo_next_lsn must be a positive integer or None, got {undo_next_lsn!r}"
                )
            if record_type != LogRecordType.CLR:
                raise InvalidLogRecordError(
                    f"undo_next_lsn is only allowed for CLR records, got {record_type.value}"
                )
            if undo_next_lsn >= lsn:
                raise InvalidLogRecordError(
                    f"undo_next_lsn ({undo_next_lsn}) must be strictly less than record LSN ({lsn})"
                )

        if record_type == LogRecordType.UPDATE:
            if transaction_id is None:
                raise InvalidLogRecordError("UPDATE log record must include transaction_id")
            if page_id is None or not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
                raise InvalidLogRecordError(f"UPDATE log record requires non-negative page_id, got {page_id!r}")
            if offset is None or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise InvalidLogRecordError(f"UPDATE log record requires non-negative offset, got {offset!r}")
            if offset >= PAGE_SIZE:
                raise InvalidLogRecordError(f"Offset {offset} is beyond PAGE_SIZE {PAGE_SIZE}")

            if before_image is None or not isinstance(before_image, (bytes, bytearray, memoryview)):
                raise InvalidLogRecordError("UPDATE log record requires bytes-like before_image")
            if after_image is None or not isinstance(after_image, (bytes, bytearray, memoryview)):
                raise InvalidLogRecordError("UPDATE log record requires bytes-like after_image")

            before_bytes = bytes(before_image)
            after_bytes = bytes(after_image)

            if len(before_bytes) == 0 and len(after_bytes) == 0:
                raise InvalidLogRecordError("UPDATE log record before/after image cannot both be empty")

            if offset + len(after_bytes) > PAGE_SIZE:
                raise InvalidLogRecordError(
                    f"UPDATE payload range [{offset} : {offset + len(after_bytes)}] exceeds page size {PAGE_SIZE}"
                )
            if offset + len(before_bytes) > PAGE_SIZE:
                raise InvalidLogRecordError(
                    f"UPDATE before_image range [{offset} : {offset + len(before_bytes)}] exceeds page size {PAGE_SIZE}"
                )

            self._page_id: Optional[int] = page_id
            self._offset: Optional[int] = offset
            self._before_image: Optional[bytes] = before_bytes
            self._after_image: Optional[bytes] = after_bytes
            self._checkpoint_payload: Optional[bytes] = None
            self._undo_next_lsn: Optional[int] = None

        elif record_type == LogRecordType.CLR:
            if transaction_id is None:
                raise InvalidLogRecordError("CLR log record must include transaction_id")
            if page_id is None or not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
                raise InvalidLogRecordError(f"CLR log record requires non-negative page_id, got {page_id!r}")
            if offset is None or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise InvalidLogRecordError(f"CLR log record requires non-negative offset, got {offset!r}")
            if offset >= PAGE_SIZE:
                raise InvalidLogRecordError(f"Offset {offset} is beyond PAGE_SIZE {PAGE_SIZE}")

            comp_image = after_image if after_image is not None else before_image
            if comp_image is None or not isinstance(comp_image, (bytes, bytearray, memoryview)):
                raise InvalidLogRecordError("CLR log record requires bytes-like compensation image")

            comp_bytes = bytes(comp_image)
            if len(comp_bytes) == 0:
                raise InvalidLogRecordError("CLR compensation image cannot be empty")
            if offset + len(comp_bytes) > PAGE_SIZE:
                raise InvalidLogRecordError(
                    f"CLR payload range [{offset} : {offset + len(comp_bytes)}] exceeds page size {PAGE_SIZE}"
                )

            self._page_id = page_id
            self._offset = offset
            self._before_image = None
            self._after_image = comp_bytes
            self._checkpoint_payload = None
            self._undo_next_lsn = undo_next_lsn

        elif record_type == LogRecordType.CHECKPOINT_END:
            self._page_id = None
            self._offset = None
            self._before_image = None
            self._after_image = None
            self._checkpoint_payload = bytes(checkpoint_payload) if checkpoint_payload is not None else b""
            self._undo_next_lsn = None
        else:
            self._page_id = None
            self._offset = None
            self._before_image = None
            self._after_image = None
            self._checkpoint_payload = None
            self._undo_next_lsn = None

        self._lsn: int = lsn
        self._record_type: LogRecordType = record_type
        self._transaction_id: Optional[int] = transaction_id
        self._prev_lsn: Optional[int] = prev_lsn


    @property
    def lsn(self) -> int:
        """Return the Log Sequence Number."""
        return self._lsn

    @property
    def record_type(self) -> LogRecordType:
        """Return the record type."""
        return self._record_type

    @property
    def transaction_id(self) -> Optional[int]:
        """Return the associated transaction ID."""
        return self._transaction_id

    @property
    def prev_lsn(self) -> Optional[int]:
        """Return the previous LSN for this transaction (or begin LSN for checkpoint)."""
        return self._prev_lsn

    @property
    def checkpoint_begin_lsn(self) -> Optional[int]:
        """Return the begin LSN for CHECKPOINT_END records."""
        if self._record_type == LogRecordType.CHECKPOINT_END:
            return self._prev_lsn
        return None

    @property
    def checkpoint_payload(self) -> Optional[bytes]:
        """Return the serialized checkpoint payload for CHECKPOINT_END records."""
        return self._checkpoint_payload

    @property
    def page_id(self) -> Optional[int]:
        """Return the target page ID for UPDATE records."""
        return self._page_id

    @property
    def offset(self) -> Optional[int]:
        """Return the byte offset for UPDATE records."""
        return self._offset

    @property
    def before_image(self) -> Optional[bytes]:
        """Return the before-image bytes for UPDATE records."""
        return self._before_image

    @property
    def after_image(self) -> Optional[bytes]:
        """Return the after-image bytes for UPDATE records or compensation image for CLRs."""
        return self._after_image

    @property
    def compensation_image(self) -> Optional[bytes]:
        """Return the compensation image bytes for CLR records."""
        return self._after_image

    @property
    def undo_next_lsn(self) -> Optional[int]:
        """Return the next LSN to undo for this transaction (for CLR records)."""
        return self._undo_next_lsn

    def serialize(self) -> bytes:
        """Serialize this log record to deterministic binary format.

        Format Layout:
            - Header (33 bytes):
                - magic (4 bytes): b'WALR'
                - total_length (4 bytes, uint32)
                - lsn (8 bytes, uint64)
                - prev_lsn (8 bytes, uint64, 0 if None)
                - transaction_id (8 bytes, int64, 0 if None)
                - type_code (1 byte, uint8)
            - Payload (Variable):
                For UPDATE:
                    - page_id (4 bytes, uint32)
                    - offset (4 bytes, uint32)
                    - before_len (4 bytes, uint32)
                    - before_image (before_len bytes)
                    - after_len (4 bytes, uint32)
                    - after_image (after_len bytes)
                For CLR:
                    - page_id (4 bytes, uint32)
                    - offset (4 bytes, uint32)
                    - undo_next_lsn (8 bytes, uint64, 0 if None)
                    - comp_len (4 bytes, uint32)
                    - compensation_image (comp_len bytes)
                For CHECKPOINT_END:
                    - checkpoint_payload (variable bytes)
            - Trailer (4 bytes):
                - crc32 (4 bytes, uint32) over all bytes prior to crc32

        Returns:
            Raw bytes representing the serialized log record.
        """
        type_code = _TYPE_TO_CODE[self._record_type]
        prev_lsn_val = self._prev_lsn if self._prev_lsn is not None else 0
        tx_id_val = self._transaction_id if self._transaction_id is not None else 0

        payload = bytearray()
        if self._record_type == LogRecordType.UPDATE:
            assert self._page_id is not None
            assert self._offset is not None
            assert self._before_image is not None
            assert self._after_image is not None

            payload += struct.pack(
                ">IIII",
                self._page_id,
                self._offset,
                len(self._before_image),
                len(self._after_image),
            )
            payload += self._before_image
            payload += self._after_image
        elif self._record_type == LogRecordType.CLR:
            assert self._page_id is not None
            assert self._offset is not None
            assert self._after_image is not None

            undo_next_val = self._undo_next_lsn if self._undo_next_lsn is not None else 0
            payload += struct.pack(
                ">IIQI",
                self._page_id,
                self._offset,
                undo_next_val,
                len(self._after_image),
            )
            payload += self._after_image
        elif self._record_type == LogRecordType.CHECKPOINT_END:
            if self._checkpoint_payload:
                payload += self._checkpoint_payload

        # Header without total_length (8 bytes: lsn, 8 bytes: prev_lsn, 8 bytes: tx_id, 1 byte: type) = 25 bytes
        # Plus 4 bytes magic + 4 bytes total_length = 33 bytes header
        # Plus 4 bytes CRC = total_length = 33 + len(payload) + 4
        total_length = 33 + len(payload) + 4

        header = bytearray()
        header += WAL_RECORD_MAGIC
        header += struct.pack(">IQQqB", total_length, self._lsn, prev_lsn_val, tx_id_val, type_code)
        header += payload

        # Calculate CRC32 over header + payload
        crc = zlib.crc32(header) & 0xFFFFFFFF
        header += struct.pack(">I", crc)

        return bytes(header)

    @classmethod
    def deserialize(cls, data: bytes, offset: int = 0) -> Tuple["LogRecord", int]:
        """Deserialize a log record from raw binary data at a given offset.

        Args:
            data: Binary buffer containing log data.
            offset: Starting byte offset in data.

        Returns:
            Tuple of (deserialized LogRecord, total_bytes_consumed).

        Raises:
            LogCorruptionError: If magic, length, checksum, or structure is invalid.
        """
        remaining = len(data) - offset
        if remaining < MIN_RECORD_SIZE:
            raise LogCorruptionError(
                f"Truncated WAL record: expected at least {MIN_RECORD_SIZE} bytes, got {remaining}"
            )

        magic = data[offset : offset + 4]
        if magic != WAL_RECORD_MAGIC:
            raise LogCorruptionError(
                f"Invalid WAL record magic: expected {WAL_RECORD_MAGIC!r}, got {magic!r}"
            )

        total_length, lsn, prev_lsn_val, tx_id_val, type_code = struct.unpack_from(
            ">IQQqB", data, offset + 4
        )

        if total_length < MIN_RECORD_SIZE:
            raise LogCorruptionError(
                f"Corrupt record total_length: {total_length} < minimum {MIN_RECORD_SIZE}"
            )

        if remaining < total_length:
            raise LogCorruptionError(
                f"Truncated record payload: expected {total_length} bytes, only {remaining} available"
            )

        record_bytes = data[offset : offset + total_length]
        body = record_bytes[:-4]
        expected_crc, = struct.unpack(">I", record_bytes[-4:])
        actual_crc = zlib.crc32(body) & 0xFFFFFFFF

        if actual_crc != expected_crc:
            raise LogCorruptionError(
                f"WAL record CRC32 mismatch at LSN {lsn}: computed 0x{actual_crc:08X}, expected 0x{expected_crc:08X}"
            )

        if type_code not in _CODE_TO_TYPE:
            raise LogCorruptionError(f"Unknown log record type code: {type_code}")

        record_type = _CODE_TO_TYPE[type_code]
        prev_lsn = prev_lsn_val if prev_lsn_val > 0 else None
        tx_id = tx_id_val if tx_id_val > 0 else None

        if record_type == LogRecordType.UPDATE:
            payload_offset = offset + 33
            if total_length < 33 + 16 + 4:  # 33 header + 16 (page, offset, blen, alen) + 4 crc
                raise LogCorruptionError("Truncated UPDATE record payload")

            page_id, page_offset, before_len, after_len = struct.unpack_from(
                ">IIII", data, payload_offset
            )
            expected_payload_len = 16 + before_len + after_len
            if total_length != 33 + expected_payload_len + 4:
                raise LogCorruptionError(
                    f"Corrupt UPDATE length: expected {33 + expected_payload_len + 4}, got {total_length}"
                )

            data_start = payload_offset + 16
            before_image = data[data_start : data_start + before_len]
            after_image = data[data_start + before_len : data_start + before_len + after_len]

            try:
                record = cls(
                    lsn=lsn,
                    record_type=record_type,
                    transaction_id=tx_id,
                    prev_lsn=prev_lsn,
                    page_id=page_id,
                    offset=page_offset,
                    before_image=before_image,
                    after_image=after_image,
                )
            except InvalidLogRecordError as exc:
                raise LogCorruptionError(f"Invalid record data within WAL: {exc}") from exc
        elif record_type == LogRecordType.CLR:
            payload_offset = offset + 33
            if total_length < 33 + 20 + 4:  # 33 header + 20 (page, offset, undo_next_lsn, comp_len) + 4 crc
                raise LogCorruptionError("Truncated CLR record payload")

            page_id, page_offset, undo_next_val, comp_len = struct.unpack_from(
                ">IIQI", data, payload_offset
            )
            expected_payload_len = 20 + comp_len
            if total_length != 33 + expected_payload_len + 4:
                raise LogCorruptionError(
                    f"Corrupt CLR length: expected {33 + expected_payload_len + 4}, got {total_length}"
                )

            data_start = payload_offset + 20
            comp_image = data[data_start : data_start + comp_len]
            undo_next = undo_next_val if undo_next_val > 0 else None

            try:
                record = cls(
                    lsn=lsn,
                    record_type=record_type,
                    transaction_id=tx_id,
                    prev_lsn=prev_lsn,
                    page_id=page_id,
                    offset=page_offset,
                    after_image=comp_image,
                    undo_next_lsn=undo_next,
                )
            except InvalidLogRecordError as exc:
                raise LogCorruptionError(f"Invalid record data within WAL: {exc}") from exc
        elif record_type == LogRecordType.CHECKPOINT_END:
            payload_offset = offset + 33
            payload_len = total_length - 33 - 4
            checkpoint_payload = data[payload_offset : payload_offset + payload_len]
            try:
                record = cls(
                    lsn=lsn,
                    record_type=record_type,
                    transaction_id=tx_id,
                    prev_lsn=prev_lsn,
                    checkpoint_payload=checkpoint_payload,
                )
            except InvalidLogRecordError as exc:
                raise LogCorruptionError(f"Invalid record data within WAL: {exc}") from exc
        else:
            try:
                record = cls(
                    lsn=lsn,
                    record_type=record_type,
                    transaction_id=tx_id,
                    prev_lsn=prev_lsn,
                )
            except InvalidLogRecordError as exc:
                raise LogCorruptionError(f"Invalid record data within WAL: {exc}") from exc

        return record, total_length

    def to_dict(self) -> Dict[str, Any]:
        """Convert record metadata and payload into dictionary format."""
        result: Dict[str, Any] = {
            "lsn": self._lsn,
            "record_type": self._record_type.value,
            "transaction_id": self._transaction_id,
            "prev_lsn": self._prev_lsn,
        }
        if self._record_type == LogRecordType.UPDATE:
            result.update({
                "page_id": self._page_id,
                "offset": self._offset,
                "before_image_len": len(self._before_image) if self._before_image else 0,
                "after_image_len": len(self._after_image) if self._after_image else 0,
            })
        elif self._record_type == LogRecordType.CLR:
            result.update({
                "page_id": self._page_id,
                "offset": self._offset,
                "undo_next_lsn": self._undo_next_lsn,
                "compensation_image_len": len(self._after_image) if self._after_image else 0,
            })
        elif self._record_type == LogRecordType.CHECKPOINT_END:
            result.update({
                "checkpoint_begin_lsn": self._prev_lsn,
                "payload_len": len(self._checkpoint_payload) if self._checkpoint_payload else 0,
            })
        return result

    def __eq__(self, other: object) -> bool:
        """Check equality with another LogRecord."""
        if not isinstance(other, LogRecord):
            return False
        return (
            self._lsn == other._lsn
            and self._record_type == other._record_type
            and self._transaction_id == other._transaction_id
            and self._prev_lsn == other._prev_lsn
            and self._page_id == other._page_id
            and self._offset == other._offset
            and self._before_image == other._before_image
            and self._after_image == other._after_image
            and self._checkpoint_payload == other._checkpoint_payload
            and self._undo_next_lsn == other._undo_next_lsn
        )

    def __repr__(self) -> str:
        """String representation of LogRecord."""
        if self._record_type == LogRecordType.UPDATE:
            return (
                f"LogRecord(LSN={self._lsn}, TYPE={self._record_type.value}, "
                f"TX={self._transaction_id}, PREV_LSN={self._prev_lsn}, "
                f"page_id={self._page_id}, offset={self._offset})"
            )
        if self._record_type == LogRecordType.CLR:
            return (
                f"LogRecord(LSN={self._lsn}, TYPE={self._record_type.value}, "
                f"TX={self._transaction_id}, PREV_LSN={self._prev_lsn}, "
                f"page_id={self._page_id}, offset={self._offset}, "
                f"undo_next_lsn={self._undo_next_lsn})"
            )
        if self._record_type == LogRecordType.CHECKPOINT_END:
            return (
                f"LogRecord(LSN={self._lsn}, TYPE={self._record_type.value}, "
                f"CHECKPOINT_BEGIN_LSN={self._prev_lsn}, "
                f"payload_len={len(self._checkpoint_payload) if self._checkpoint_payload else 0})"
            )
        if self._record_type == LogRecordType.CHECKPOINT_BEGIN:
            return f"LogRecord(LSN={self._lsn}, TYPE={self._record_type.value})"
        return (
            f"LogRecord(LSN={self._lsn}, TYPE={self._record_type.value}, "
            f"TX={self._transaction_id}, PREV_LSN={self._prev_lsn})"
        )
