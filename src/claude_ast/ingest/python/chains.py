"""Chain resolution — walk an external, module-rooted call chain through the typeshed tables.

``refs`` captures a call whose callee is a name-rooted attribute chain as a flat dotted string
(``os.path.join``, ``sys.stdout.getvalue``). Syntactic binding mints the whole chain as a
*definite* external node — correct when every hop is module namespace (``os.path`` is a
submodule, ``join`` a module function), but WRONG once the chain crosses into a *value* whose
members are type-dependent (``sys.stdout`` is a ``TextIO``, so ``getvalue`` exists only if that
type has it — and it does not; a ``StringIO`` patched in at runtime is unknowable statically).

This threads the chain component by component. The asymmetry is the whole point, and it protects
recall while fixing finding #2:

- On a **module**, an *unknown* attribute KEEPS the definite edge — we may simply not have
  extracted that member, so we must not regress a real module fact. We only leave module namespace
  through a *known* value/function-return.
- Once a **known value** attribute is crossed, the tail is type-dependent, so it can only
  DOWNGRADE to a ``possible``/STUB edge on the value's type (member present) or DECLINE (member
  absent, or the type is unmodeled) — never stay definite.

``os.path.join`` keeps; ``sys.stdout.getvalue`` declines. Consulted at resolve time via the
``StubProvider`` seam, so it is a pure hermetic lookup (no import, no live env).
"""

from __future__ import annotations

from .stubs import StubProvider

KEEP = "keep"  # leave the definite external edge exactly as syntactic binding minted it
_SELF = "Self"  # the generator's sentinel for a `Self` return — resolves to the receiver type


def _member(typ: str, name: str, stubs: StubProvider) -> tuple[str, str] | None:
    """A type member with covariant ``Self`` resolved: a method typed to return ``Self`` yields
    the *receiver* type (``Path.cwd().parent`` -> ``Path``, not the defining ``PurePath``)."""
    m = stubs.type_member(typ, name)
    if m is None:
        return None
    kind, result = m
    return (kind, typ if result == _SELF else result)


def resolve_external_chain(
    dotted: str, stubs: StubProvider, is_call: bool = True
) -> str | tuple[str, str] | None:
    """Decide an external CALL or READ chain. Returns ``"keep"`` (emit the definite external edge
    unchanged), ``("stub", target)`` (emit a MEDIUM/STUB edge to the value-type member instead),
    or ``None`` (decline — the member is type-dependent and unconfirmable, e.g. #2).

    ``is_call`` governs the one place the two diverge: the terminal member of a *module value*
    (``os.EX_OK``). *Calling* it is type-dependent (the value's ``__call__`` is unknown) so a call
    declines; *reading* it is a definite reference to that module member, so a read KEEPs. Every
    other rung — module facts, submodules, and members reached on a value's *type* — is identical
    (a value-type member stays a MEDIUM/STUB edge whether it is then called or merely read)."""
    parts = dotted.split(".")
    if not stubs.has_module(parts[0]):
        return KEEP  # no shape data for this library -> leave the definite edge as-is
    state, ref = "mod", parts[0]
    for k in range(1, len(parts)):
        comp = parts[k]
        last = k == len(parts) - 1
        if state == "mod":
            if stubs.has_module(f"{ref}.{comp}"):
                ref = f"{ref}.{comp}"  # a submodule -> still module namespace
                continue
            member = stubs.module_member(ref, comp)
            if member is None:
                return KEEP  # unknown module attribute -> a definite module fact, don't regress
            kind, typ = member
            if kind in ("func", "class") and last:
                return KEEP  # a module function / class -> definite external (called or read)
            if kind == "value" and last:
                # read a module value -> definite reference; call it -> type-dependent, decline.
                return None if is_call else KEEP
            state, ref = _advance(kind, typ, f"{ref}.{comp}", stubs)
        elif state == "type":
            member = _member(ref, comp, stubs)
            if member is None:
                return None  # member absent on the known type (or type unmodeled) -> decline (#2)
            kind, typ = member
            if last:
                return ("stub", f"{ref}.{comp}")  # member exists on the value's type -> MEDIUM STUB
            state, ref = _advance(kind, typ, ref, stubs)
        else:  # opaque -> the type is unmodeled, cannot thread further
            return None
    return KEEP


def _advance(kind: str, typ: str, submodule: str, stubs: StubProvider) -> tuple[str, str]:
    """The state after accessing a non-terminal member: the type it yields (or opaque)."""
    if kind == "submodule":
        return ("mod", typ if stubs.has_module(typ) else submodule)
    return ("type", typ) if typ else ("opaque", "")


def chain_return_type(dotted: str, stubs: StubProvider) -> str | None:
    """The type produced by *calling* the external chain ``dotted`` — the receiver type for one
    more hop of chaining (``Path.cwd()`` returns ``pathlib.Path``, so ``.exists()`` resolves on
    it). ``None`` when it isn't a resolvable callable, or its return type is OPAQUE/unmodeled."""
    parts = dotted.split(".")
    if not stubs.has_module(parts[0]):
        return None
    state, ref, final = "mod", parts[0], None
    for k in range(1, len(parts)):
        comp = parts[k]
        if state == "mod":
            if stubs.has_module(f"{ref}.{comp}"):
                ref = f"{ref}.{comp}"
                continue
            member = stubs.module_member(ref, comp)
            if member is None:
                return None
            final, (state, ref) = member, _advance(member[0], member[1], f"{ref}.{comp}", stubs)
        elif state == "type":
            member = _member(ref, comp, stubs)
            if member is None:
                return None
            final, (state, ref) = member, _advance(member[0], member[1], ref, stubs)
        else:
            return None
    if final is None:
        return None
    kind, typ = final
    if kind == "class":
        return typ  # calling a class constructs an instance of it
    return typ or None if kind in ("func", "method") else None  # a value member isn't callable


def resolve_call_chain(root: str, members: tuple[str, ...], stubs: StubProvider) -> str | None:
    """The target member of a call-return chain, or ``None`` to decline. ``root`` is the receiver
    call resolved to its external qualname (``re.compile``); ``members`` the trailing members
    reached on its return (``("match", "group")``). Threads the receiver's return type through the
    leading members — each advances to that member's result type — then looks up the last member
    on the resulting type. Any hop with an absent or OPAQUE result declines the whole chain."""
    typ = chain_return_type(root, stubs)
    if typ is None:
        return None
    for name in members[:-1]:
        member = _member(typ, name, stubs)
        if member is None or not member[1]:
            return None
        typ = member[1]  # a called method's return, an accessed property/value's type
    return f"{typ}.{members[-1]}" if stubs.type_member(typ, members[-1]) is not None else None
