"""Service layer for the golden index fixture."""

from .core import Base, hub


class Service(Base):
    """A Base subclass that uses the hub."""

    def run(self) -> int:
        """Delegate to the hub."""
        return hub()

    def store(self) -> None:
        """Calls ``self.save`` — resolves cross-file to the inherited ``Base.save``."""
        self.save()


def start() -> int:
    """Entry point that calls the hub."""
    return hub()


def handle(service: Service) -> int:
    """Annotated receiver: `service: Service` -> Service.run at the possible tier."""
    return service.run()


def bootstrap() -> int:
    """Construction inference: `s = Service()` -> Service.run at the possible tier."""
    s = Service()
    return s.run()
