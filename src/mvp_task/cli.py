"""Small setup and import CLI for repeatable local demos."""

from __future__ import annotations

import argparse
from pathlib import Path

from .db import initialize
from .ingest import import_file


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    importer = sub.add_parser("import")
    importer.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    initialize()
    if args.command == "import":
        for path in args.files:
            print(path.name, import_file(path.name, path.read_bytes()))


if __name__ == "__main__":
    main()
