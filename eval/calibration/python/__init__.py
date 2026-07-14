"""The Python calibration backend — how the two neutral oracle protocols are answered here.

Everything language-specific about scoring claude-ast's Python edges lives behind this
package, mirroring the engine's ``ingest/python`` seam: the runtime oracle traces with
``sys.setprofile`` and reads ``co_qualname``; the static oracle uses ``importlib`` /
``builtins`` / the ``__mro__`` / the ``.py`` file layout; the id helpers know the dotted
symbol-id shape. The neutral layer (``edges`` / ``verdicts`` / ``report``) imports none of it.
"""
