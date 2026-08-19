"""HTTP routers: path, request validation, and status codes only.

Everything here calls into ``core/`` or a port directly (via ``deps.py``)
and translates the result to an HTTP response — no rule that could be
described without mentioning HTTP belongs in this package.
"""
