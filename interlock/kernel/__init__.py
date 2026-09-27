"""Kernel adapters. Import ``get_backend`` rather than a concrete backend."""

from .base import (
    FaceRecord,
    IngestResult,
    KernelBackend,
    MassProperties,
    OccurrenceNode,
    SolidRecord,
    TopologyCounts,
    identity_transform,
    make_transform,
)

__all__ = [
    "FaceRecord",
    "IngestResult",
    "KernelBackend",
    "MassProperties",
    "OccurrenceNode",
    "SolidRecord",
    "TopologyCounts",
    "available_backends",
    "get_backend",
    "identity_transform",
    "make_transform",
]

_CACHE: dict = {}


def get_backend(name: str = "auto"):
    if name in _CACHE:
        return _CACHE[name]
    from .occt import OcctBackend

    if name in ("auto", "occt"):
        backend = OcctBackend()
        if backend.available():
            _CACHE[name] = backend
            return backend
        if name == "occt":
            raise RuntimeError(f"OCCT backend unavailable: {backend.import_error()}")
    raise RuntimeError(f"no kernel backend named {name!r} is available")


def available_backends() -> dict[str, bool]:
    from .occt import OcctBackend

    return {"occt": OcctBackend().available()}
