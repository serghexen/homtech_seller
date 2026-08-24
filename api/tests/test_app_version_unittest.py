"""Проверяет единую release-версию API и интерфейса Seller."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from app import app


class AppVersionTests(unittest.TestCase):
    def test_api_and_web_versions_match(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        package_metadata = json.loads((project_root / "web" / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(app.version, package_metadata["version"])


if __name__ == "__main__":
    unittest.main()
