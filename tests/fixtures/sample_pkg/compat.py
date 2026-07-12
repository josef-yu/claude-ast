"""Version-gated definitions — a same-qualname collision fixture."""

import sys

if sys.version_info >= (3, 13):

    def feature() -> str:
        """New implementation."""
        return "new"

else:

    def feature() -> str:
        """Legacy implementation."""
        return "old"
