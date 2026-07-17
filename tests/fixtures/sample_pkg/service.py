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


def dispatch(obj) -> None:
    """Untyped receiver: name-matches `persist` (heuristic, LOW) -> Base.persist."""
    obj.persist()


def consume(record) -> None:
    """Untyped parameter; call sites report the concrete type they pass in."""


def feed() -> None:
    """Passes a constructed ``Service`` -> `consume` RECEIVES_ARG Service (a definite observation)."""
    consume(Service())


class Bootstrapper:
    """Holds a constructed ``Service`` as an instance attribute — a chain threads through it."""

    def __init__(self) -> None:
        self.service = Service()  # a construction-typed instance attribute

    def launch(self) -> int:
        """`self.service` (Service, inferred) . `run` -> Service.run at the possible tier."""
        return self.service.run()
