from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from energygrid_bill_downloader.config import load_runtime_config
from energygrid_bill_downloader.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def raw(self) -> dict[str, object]:
        return {
            "portal_url": "http://127.0.0.1:1",
            "archive_root": str(self.root / "archive"),
            "state_path": str(self.root / "state" / "bills.sqlite3"),
            "temp_root": str(self.root / "temp"),
            "log_root": str(self.root / "logs"),
        }

    def test_valid_config_resolves_external_paths_and_bounds(self) -> None:
        config = load_runtime_config(self.raw(), checkout_root=Path.cwd())
        self.assertEqual(config.timeout_seconds, 30)
        self.assertEqual(config.max_attempts, 2)
        self.assertTrue(config.archive_root.is_absolute())

    def test_paths_inside_checkout_are_rejected(self) -> None:
        raw = self.raw()
        raw["archive_root"] = str(Path.cwd() / "private-archive")
        with self.assertRaises(ConfigError):
            load_runtime_config(raw, checkout_root=Path.cwd())

    def test_private_runtime_roots_must_not_overlap(self) -> None:
        raw = self.raw()
        raw["temp_root"] = str(self.root / "archive" / "temp")
        with self.assertRaises(ConfigError):
            load_runtime_config(raw, checkout_root=Path.cwd())
        raw = self.raw()
        raw["browser_cache_path"] = str(self.root / "archive" / "browser")
        with self.assertRaises(ConfigError):
            load_runtime_config(raw, checkout_root=Path.cwd())

    def test_invalid_url_and_bounds_are_rejected(self) -> None:
        raw = self.raw()
        raw["portal_url"] = "file:///private"
        with self.assertRaises(ConfigError):
            load_runtime_config(raw, checkout_root=Path.cwd())
        raw = self.raw()
        raw["timeout_seconds"] = 0
        with self.assertRaises(ConfigError):
            load_runtime_config(raw, checkout_root=Path.cwd())

    def test_preflight_creates_only_runtime_parents_and_requires_archive(self) -> None:
        config = load_runtime_config(self.raw(), checkout_root=Path.cwd())
        with self.assertRaises(ConfigError):
            config.preflight()
        config.archive_root.mkdir()
        config.preflight()
        self.assertTrue(config.state_path.parent.is_dir())
        self.assertTrue(config.temp_root.is_dir())
        self.assertTrue(config.log_root.is_dir())


if __name__ == "__main__":
    unittest.main()
