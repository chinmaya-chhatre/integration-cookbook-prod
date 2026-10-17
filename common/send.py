"""Send a signed example event to a running HookScope: the quickest way to try a recipe.

    python -m common.send stripe_invoice_paid amount_paid=500000 currency=usd
    python -m common.send github_pull_request action=closed merged=true --url http://localhost:8000

The first argument is a builder from ``common.fixtures``; ``name=value`` arguments are passed to it
(numbers and true/false are converted). The event goes to ``<url>/hooks/<provider>``, where the
provider is the part of the builder's name before the first underscore. Run ``make env`` and
``make up`` first so HookScope knows the demo secrets.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

from . import fixtures
from .signing import SignedRequest


def coerce(value: str) -> object:
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def build(name: str, assignments: list[str]) -> SignedRequest:
    builder = getattr(fixtures, name, None)
    if not callable(builder) or name.startswith("_") or name in ("dumps",):
        available = ", ".join(
            sorted(n for n in dir(fixtures) if "_" in n and not n.startswith("_") and n[0].islower())
        )
        raise SystemExit(f"unknown fixture '{name}'. Available: {available}")
    kwargs = {}
    for item in assignments:
        key, separator, value = item.partition("=")
        if not separator:
            raise SystemExit(f"expected name=value, got '{item}'")
        kwargs[key] = coerce(value)
    return builder(**kwargs)


def send(request: SignedRequest, url: str, timeout: float = 10.0) -> tuple[int, str]:
    http = urllib.request.Request(url, data=request.body, headers=request.all_headers(), method="POST")
    try:
        with urllib.request.urlopen(http, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:  # a rejected delivery is still an answer worth showing
        return error.code, error.read().decode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m common.send", description=__doc__.split("\n\n")[0])
    parser.add_argument("fixture", help="a builder in common.fixtures, e.g. stripe_invoice_paid")
    parser.add_argument("assignments", nargs="*", metavar="name=value")
    parser.add_argument(
        "--url", default="http://localhost:8000", help="HookScope's address (default %(default)s)"
    )
    args = parser.parse_args(argv)
    request = build(args.fixture, args.assignments)
    provider = args.fixture.split("_", 1)[0]
    target = request.url or f"{args.url.rstrip('/')}/hooks/{provider}"
    status, body = send(request, target)
    print(f"POST {target} -> {status}")
    print(json.dumps(json.loads(body), indent=2) if body.startswith("{") else body)
    return 0 if status < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
