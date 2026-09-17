"""Custom exception classes for the Checkpointing and Dirty Page Table subsystem."""


class CheckpointError(Exception):
    """Base exception class for all checkpointing and DPT errors."""


class CheckpointCorruptionError(CheckpointError):
    """Raised when checkpoint data, header, or payload fails integrity validation."""


class InvalidCheckpointError(CheckpointError):
    """Raised when checkpoint structure, LSN references, or metadata are invalid."""


class CheckpointManagerClosedError(CheckpointError):
    """Raised when operations are attempted on a closed CheckpointManager."""
