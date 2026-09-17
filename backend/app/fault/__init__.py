"""Fault injection subsystem for controlled crash simulation in ARIES crash recovery."""

from app.fault.constants import DEFAULT_CRASH_EXIT_CODE, FaultPoint
from app.fault.exceptions import FaultInjectionError, InvalidFaultPointError
from app.fault.fault_injector import FaultInjector, fault_injector, fault_scope

__all__ = [
    "DEFAULT_CRASH_EXIT_CODE",
    "FaultPoint",
    "FaultInjectionError",
    "InvalidFaultPointError",
    "FaultInjector",
    "fault_injector",
    "fault_scope",
]
