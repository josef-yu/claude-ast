"""Core definitions for the golden index fixture."""

BASE_LIMIT = 10


class Base:
    """Base class for services."""

    def save(self) -> None:
        """Persist the object."""

    def persist(self) -> None:
        """Delegates to ``self.save`` — a value-typed (possible) self-call."""
        self.save()


def hub() -> int:
    """Central function that many callers use."""
    return BASE_LIMIT


def shadowed() -> int:
    """A local `hub` shadows the module function — must not bind as a caller."""

    def hub() -> int:
        return 0

    return hub()
