"""Не позволяет случайно изменить устаревший workspace-тариф."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "Workspace-wide тарифы больше не используются. "
        "Запустите scripts/set_connection_plan.py CONNECTION_ID PLAN_CODE.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
