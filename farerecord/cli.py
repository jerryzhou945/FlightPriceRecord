"""命令行入口（各子命令在后续里程碑中实现）。"""

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="farerecord", description="航价记")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args(argv)
    if args.version:
        from farerecord import __version__

        print(__version__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
