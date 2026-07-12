"""Service layer for the golden index fixture."""

from sample_pkg.core import Base, hub


class Service(Base):
    """A Base subclass that uses the hub."""

    def run(self) -> int:
        """Delegate to the hub."""
        return hub()


def start() -> int:
    """Entry point that calls the hub."""
    return hub()
