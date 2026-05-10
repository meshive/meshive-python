import argparse

from .._version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meshive",
        description="Meshive GPU Cloud CLI",
        epilog=(
            "Examples:\n"
            "  meshive --version       Show version\n"
            "  meshive --help          Show this help message\n"
            "\n"
            "Docs: https://github.com/meshive/meshive-python"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
