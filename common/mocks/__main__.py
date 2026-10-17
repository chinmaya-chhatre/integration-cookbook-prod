"""Run one mock in the foreground: ``python -m common.mocks slack --port 9000``."""

from __future__ import annotations

import argparse

from . import CrmMock, JiraMock, SlackMock, WarehouseMock

MOCKS = {"jira": JiraMock, "crm": CrmMock, "warehouse": WarehouseMock, "slack": SlackMock}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m common.mocks", description=__doc__)
    parser.add_argument("mock", choices=sorted(MOCKS))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    print(f"{args.mock} mock listening on {args.host}:{args.port}", flush=True)
    MOCKS[args.mock]().serve_forever(args.host, args.port)


if __name__ == "__main__":
    main()
