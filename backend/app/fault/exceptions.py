"""Custom exceptions for the Fault Injection subsystem."""


class FaultInjectionError(Exception):
    """Base exception for all fault injection errors."""


class InvalidFaultPointError(FaultInjectionError):
    """Raised when an invalid or unrecognized fault point identifier is provided."""
