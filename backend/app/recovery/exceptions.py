"""Exception classes for the Crash Recovery subsystem."""


class RecoveryError(Exception):
    """Base exception class for all recovery errors."""


class AnalysisError(RecoveryError):
    """Raised when an error occurs during the ARIES Analysis phase."""


class RedoError(RecoveryError):
    """Raised when an error occurs during the ARIES Redo phase."""


class UndoError(RecoveryError):
    """Raised when an error occurs during the ARIES Undo phase."""


class RecoveryManagerError(RecoveryError):
    """Raised when an error occurs during crash recovery orchestration."""
