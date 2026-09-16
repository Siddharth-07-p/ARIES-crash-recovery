"""LRU (Least Recently Used) replacement policy for buffer frame management."""

from collections import OrderedDict
from typing import Optional


class LRUReplacer:
    """Tracks unpinned frames and selects the least recently used frame as victim.

    Frames with pin_count > 0 are pinned and therefore NOT present in the replacer.
    When a frame's pin_count reaches 0, it is registered in the replacer.
    When a frame is pinned again, it is removed from the replacer.
    """

    def __init__(self) -> None:
        """Initialize empty LRU replacer."""
        # Key: frame_id, Value: None (maintains insertion/access order)
        self._unpinned: OrderedDict[int, None] = OrderedDict()

    def unpin(self, frame_id: int) -> None:
        """Mark a frame as unpinned and eligible for eviction (MRU position).

        Args:
            frame_id: Physical frame ID.
        """
        if frame_id in self._unpinned:
            self._unpinned.move_to_end(frame_id)
        else:
            self._unpinned[frame_id] = None

    def pin(self, frame_id: int) -> None:
        """Remove a frame from eviction candidates because it has been pinned.

        Args:
            frame_id: Physical frame ID.
        """
        self._unpinned.pop(frame_id, None)

    def victim(self) -> Optional[int]:
        """Select and remove the least recently used frame for eviction.

        Returns:
            Frame ID of the LRU victim, or None if no frames are eligible.
        """
        if not self._unpinned:
            return None
        # popitem(last=False) pops the oldest / least recently used element
        frame_id, _ = self._unpinned.popitem(last=False)
        return frame_id

    def remove(self, frame_id: int) -> None:
        """Remove a frame from the replacer without returning it.

        Args:
            frame_id: Physical frame ID.
        """
        self._unpinned.pop(frame_id, None)

    def size(self) -> int:
        """Return the number of unpinned frames currently eligible for eviction."""
        return len(self._unpinned)

    def __contains__(self, frame_id: int) -> bool:
        """Check whether a frame is in the replacer."""
        return frame_id in self._unpinned

    def __repr__(self) -> str:
        """String representation of LRUReplacer state."""
        return f"LRUReplacer(unpinned_frames={list(self._unpinned.keys())})"
