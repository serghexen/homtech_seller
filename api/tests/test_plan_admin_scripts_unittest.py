"""Защита административного переключения тарифов от старого workspace-сценария."""

from __future__ import annotations

import inspect
import io
import unittest
from contextlib import redirect_stderr

from scripts import set_connection_plan, set_workspace_plan


class PlanAdminScriptsTests(unittest.TestCase):
    def test_legacy_workspace_script_fails_closed(self) -> None:
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(set_workspace_plan.main(), 2)
        self.assertIn("set_connection_plan.py", stderr.getvalue())

    def test_connection_script_updates_only_connection_subscription(self) -> None:
        source = inspect.getsource(set_connection_plan)

        self.assertIn("marketplace_connection_subscriptions", source)
        self.assertIn("WHERE connection.id=%s", source)
        self.assertNotIn("INSERT INTO seller.workspace_subscriptions", source)


if __name__ == "__main__":
    unittest.main()
