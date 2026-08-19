"""Write the FastAPI app's OpenAPI schema to a file — the source orval reads.

A build step for exactly one consumer, the frontend's ``make generate``,
invoked by the repository-root ``Makefile``'s ``openapi`` target.

Always builds the app on fake adapters, regardless of ``PALMIMO_ADAPTERS`` in
the invoking shell: the schema is the same either way (adapter choice affects
runtime behavior, not the Pydantic models the routers declare), and pinning
it here keeps a stray real-adapter environment variable from making this
step depend on D-Bus or real-device filesystem paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from palmimo_portal.api.app import create_app
from palmimo_portal.settings import Settings


DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "frontend" / "openapi.json"


def export_openapi(output_path: Path) -> None:
    """Build the app on fake adapters and write its OpenAPI schema to ``output_path``."""
    app = create_app(Settings(adapters="fake"))
    schema = app.openapi()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys + trailing newline: stable byte-for-byte output, so `make
    # check`'s `git diff --exit-code` is a meaningful drift gate.
    output_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    """Entry point for ``uv run python -m palmimo_portal.openapi_export [output_path]``."""
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    export_openapi(output_path)


if __name__ == "__main__":
    main()
