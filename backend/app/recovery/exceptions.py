"""Exception classes for the Crash Recovery subsystem."""


class RecoveryError(Exception):
    """Base exception class for all recovery errors."""


class AnalysisError(RecoveryError):
    """Raised when an error occurs during the ARIES Analysis phase."""
