from __future__ import annotations

import logging
import sys

from . import __version__
from .logging_config import (
    configure_logging,
    install_exception_hook,
    record_runtime_error,
)


def main() -> int:
    try:
        log_path = configure_logging()
        install_exception_hook()
    except OSError as exc:
        log_path = None
        print(f"MujTerm could not open its runtime log: {exc}", file=sys.stderr)
    try:
        from .app import MujTermApplication
    except (ImportError, ValueError) as exc:
        record_runtime_error("Runtime import failed", exc)
        print(
            "MujTerm needs GTK 3, VTE 2.91 and PyGObject. "
            f"Import failed: {exc}",
            file=sys.stderr,
        )
        return 1
    if log_path is not None:
        logging.getLogger("mujterm.startup").info(
            "Starting MujTerm %s; log=%s", __version__, log_path
        )
    return MujTermApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
