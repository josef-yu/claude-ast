"""Python symbol-id shape — the dotted-qualname assumptions the neutral layer refuses to make.

A claude-ast Python id is ``module.dotted.path`` + ``.`` + a nested ``Class.method`` qualname,
with a ``#N`` suffix disambiguating same-qualname siblings. That format is backend-owned (a
JS/TS backend may join ids differently), so the helpers that split on ``.`` or strip ``#N``
belong here, not in the neutral ``edges`` module. ``module_of`` resolves structurally against
the real module set rather than guessing where the path ends; ``resolve_object`` imports the
runtime object an id names (the shared basis of both oracles' identity checks).
"""

from __future__ import annotations

import importlib


def strip_disambiguator(qual: str) -> str:
    """Drop the tool's ``#N`` same-qualname disambiguator — a runtime trace can't see it."""
    return qual.split("#", 1)[0]


def leaf(qual: str) -> str:
    return qual.rsplit(".", 1)[-1]


def class_of(qual: str) -> str:
    return qual.rsplit(".", 1)[0]


def module_of(sid: str, modules: frozenset[str]) -> str | None:
    """The module a symbol lives in: the longest module id that prefixes ``sid``.

    Structural, not lexical — we ask which prefixes are real modules rather than guessing
    where the module path ends (``a.b.c`` could be module ``a.b`` class ``c`` or module ``a``
    class ``b`` method ``c``). Longest wins so a submodule beats its parent package.
    """
    best: str | None = None
    for m in modules:
        if (sid == m or sid.startswith(m + ".")) and (best is None or len(m) > len(best)):
            best = m
    return best


def resolve_object(sid: str, modules: frozenset[str]) -> object | None:
    """Import the runtime object a symbol id names, or ``None`` if it can't be resolved.

    In-tree ids resolve via ``module_of`` + ``getattr``; an external dotted id (``os.path.join``,
    ``abc.ABC``) by importing the longest importable prefix then ``getattr``-ing the rest. All
    failures — unimportable module, missing attribute — collapse to ``None``; a caller that needs
    to tell them apart (the static oracle's skip-vs-refute) resolves stepwise itself.
    """
    mod_name = module_of(sid, modules)
    try:
        if mod_name is not None:
            obj: object = importlib.import_module(mod_name)
            rest = sid[len(mod_name) + 1 :].split(".") if sid != mod_name else []
        else:
            parts = sid.split(".")
            obj = None
            rest = []
            for i in range(len(parts), 0, -1):
                try:
                    obj = importlib.import_module(".".join(parts[:i]))
                    rest = parts[i:]
                    break
                except Exception:
                    continue
            if obj is None:
                return None
        for attr in rest:
            obj = getattr(obj, attr)
    except Exception:
        return None
    return obj
