"""Use-case layer: rules that depend only on :mod:`palmimo_portal.ports`.

Nothing here imports FastAPI, touches the filesystem, or otherwise reaches
outside the port protocols — that is the discipline the ports-and-adapters
structure exists to hold.
"""
