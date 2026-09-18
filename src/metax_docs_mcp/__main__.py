"""Run the :mod:`metax_docs_mcp` command line interface."""

from .cli import main


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    raise SystemExit(main())
