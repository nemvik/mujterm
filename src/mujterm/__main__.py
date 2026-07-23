from __future__ import annotations

import sys


def main() -> int:
    try:
        from .app import MujTermApplication
    except (ImportError, ValueError) as exc:
        print(
            "MujTerm needs GTK 3, VTE 2.91 and PyGObject. "
            f"Import failed: {exc}",
            file=sys.stderr,
        )
        return 1
    return MujTermApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
