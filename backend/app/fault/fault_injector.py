"""Fault injector for deterministic crash and failure simulation."""

from contextlib import contextmanager
import os
from typing import Callable, Iterator, Optional, Union
from app.fault.constants import DEFAULT_CRASH_EXIT_CODE, FaultPoint
from app.fault.exceptions import InvalidFaultPointError


class FaultInjector:
    """Manages deterministic crash injection at configured database execution boundaries.

    By default, fault injection is completely disabled and incurs negligible overhead.
    When enabled for a specific FaultPoint, reaching that execution boundary triggers
    immediate process termination via os._exit() to simulate realistic power failure
    or kernel crash without running normal process exit handlers.
    """

    __slots__ = (
        "_configured_point",
        "_enabled",
        "_one_shot",
        "_exit_code",
        "_crash_action",
    )

    def __init__(self) -> None:
        """Initialize FaultInjector in disabled state."""
        self._configured_point: Optional[FaultPoint] = None
        self._enabled: bool = False
        self._one_shot: bool = True
        self._exit_code: int = DEFAULT_CRASH_EXIT_CODE
        self._crash_action: Optional[Callable[[FaultPoint], None]] = None

    @property
    def is_enabled(self) -> bool:
        """Return True if fault injection is active."""
        return self._enabled

    @property
    def configured_fault_point(self) -> Optional[FaultPoint]:
        """Return the currently configured FaultPoint, or None."""
        return self._configured_point if self._enabled else None

    @property
    def exit_code(self) -> int:
        """Return the configured process exit code."""
        return self._exit_code

    def _normalize_fault_point(self, point: Union[FaultPoint, str]) -> FaultPoint:
        """Validate and convert input to FaultPoint enum.

        Raises:
            InvalidFaultPointError: If point is not a recognized FaultPoint.
        """
        if isinstance(point, FaultPoint):
            return point
        if isinstance(point, str):
            try:
                return FaultPoint(point)
            except ValueError:
                raise InvalidFaultPointError(f"Unknown fault point: {point!r}")
        raise InvalidFaultPointError(
            f"fault_point must be FaultPoint or str, got {type(point).__name__}"
        )

    def enable(
        self,
        fault_point: Union[FaultPoint, str],
        one_shot: bool = True,
        exit_code: int = DEFAULT_CRASH_EXIT_CODE,
    ) -> None:
        """Enable crash injection at a specific fault point.

        Args:
            fault_point: The target FaultPoint enum or string identifier.
            one_shot: If True (default), disables injector after first trigger.
            exit_code: Process exit code passed to os._exit (default 42).

        Raises:
            InvalidFaultPointError: If fault_point is invalid.
        """
        norm_point = self._normalize_fault_point(fault_point)
        self._configured_point = norm_point
        self._enabled = True
        self._one_shot = bool(one_shot)
        self._exit_code = int(exit_code)

    def disable(self) -> None:
        """Disable fault injection and clear configured fault point."""
        self._enabled = False
        self._configured_point = None

    def reset(self) -> None:
        """Reset fault injector to initial clean state."""
        self.disable()
        self._one_shot = True
        self._exit_code = DEFAULT_CRASH_EXIT_CODE
        self._crash_action = None

    def set_crash_action(self, action: Optional[Callable[[FaultPoint], None]]) -> None:
        """Set a custom callback action to execute on trigger instead of os._exit.

        Used for in-process testing where actual process termination is undesirable.

        Args:
            action: Callback taking FaultPoint as argument, or None to restore os._exit.
        """
        self._crash_action = action

    def trigger(self, fault_point: Union[FaultPoint, str]) -> bool:
        """Trigger crash injection if the specified fault point is configured and enabled.

        If enabled and matching:
            - If one_shot is True, disables injector.
            - Invokes custom crash action or immediately terminates process via os._exit().

        Args:
            fault_point: The current execution boundary being reached.

        Returns:
            True if fault point was triggered (and process did not terminate), False otherwise.
        """
        if not self._enabled or self._configured_point is None:
            return False

        try:
            norm_point = self._normalize_fault_point(fault_point)
        except InvalidFaultPointError:
            return False

        if norm_point != self._configured_point:
            return False

        # One-shot consumption
        if self._one_shot:
            self.disable()

        # Execute crash
        if self._crash_action is not None:
            self._crash_action(norm_point)
            return True

        # Actual deterministic process termination
        os._exit(self._exit_code)

    def __repr__(self) -> str:
        """String representation of FaultInjector."""
        status = "enabled" if self._enabled else "disabled"
        point = self._configured_point.value if self._configured_point else "None"
        return f"FaultInjector(status='{status}', point='{point}', one_shot={self._one_shot})"


# Global default instance for application-wide boundary checks
fault_injector: FaultInjector = FaultInjector()


@contextmanager
def fault_scope(
    fault_point: Union[FaultPoint, str],
    one_shot: bool = True,
    exit_code: int = DEFAULT_CRASH_EXIT_CODE,
    injector: Optional[FaultInjector] = None,
) -> Iterator[FaultInjector]:
    """Context manager for temporarily enabling fault injection within a code block."""
    target_injector = injector if injector is not None else fault_injector
    target_injector.enable(fault_point=fault_point, one_shot=one_shot, exit_code=exit_code)
    try:
        yield target_injector
    finally:
        target_injector.disable()
