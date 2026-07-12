"""External-reference cases for the golden eval — library/stdlib targets.

These bind to imports that resolve *outside* the indexed package, so they must
surface as `definite` edges to EXTERNAL nodes (not be dropped, and not be ranked).
"""

from abc import ABC
from os.path import join


class Plugin(ABC):
    """Inherits an external (stdlib) base class."""


def build_path(name: str) -> str:
    """Calls an external (stdlib) function."""
    return join("/tmp", name)
