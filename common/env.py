"""Write the demo webhook secrets as an env file HookScope reads: ``python -m common.env > .env``.

HookScope takes one ``HOOKSCOPE_SECRET_<SOURCE>`` variable per provider. The values come from
``common.fixtures.SECRETS``, so the secrets the tests sign with and the ones the compose stack
verifies with can never drift apart. They are public demo values; never reuse them.
"""

from __future__ import annotations

from .fixtures import SECRETS


def render_env(secrets: dict[str, str] | None = None) -> str:
    lines = [f"HOOKSCOPE_SECRET_{source.upper()}={secret}" for source, secret in (secrets or SECRETS).items()]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(render_env(), end="")
