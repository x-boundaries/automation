from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from energygrid_bill_downloader.config import is_within, load_runtime_config
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
            "account_identity": "SYNTHETIC-INTENDED-ACCOUNT",
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
    def test_checkout_private_archive_exception_is_narrow_and_portable(self) -> None:
        checkout = self.root / "synthetic-checkout"
        raw = self.raw()
        private_archive = checkout / "_MandarinGallery" / "Utilities" / "EnergyGrid"
        raw["archive_root"] = str(private_archive)
        config = load_runtime_config(raw, checkout_root=checkout)
        self.assertEqual(config.archive_root, private_archive.resolve())

        raw = self.raw()
        raw["archive_root"] = str(checkout / "other-private-root")
        with self.assertRaises(ConfigError):
            load_runtime_config(raw, checkout_root=checkout)

        for key in ("state_path", "temp_root", "log_root"):
            raw = self.raw()
            raw[key] = str(checkout / "_MandarinGallery" / key)
            with self.assertRaises(ConfigError, msg=key):
                load_runtime_config(raw, checkout_root=checkout)

        raw = self.raw()
        raw["browser_cache_path"] = str(checkout / "_MandarinGallery" / "browser")
        with self.assertRaises(ConfigError):
            load_runtime_config(raw, checkout_root=checkout)

    def test_example_config_keeps_archive_exception_separate_from_runtime(self) -> None:
        example = Path(__file__).parents[1] / "config" / "energygrid.example.json"
        raw = json.loads(example.read_text(encoding="utf-8"))
        checkout = self.root / "portable-checkout"
        raw["archive_root"] = str(checkout / "_MandarinGallery" / "Utilities" / "EnergyGrid")
        for key, value in {
            "state_path": self.root / "runtime-state" / "state.sqlite3",
            "temp_root": self.root / "runtime-temp",
            "log_root": self.root / "runtime-logs",
            "browser_cache_path": self.root / "runtime-browser" / "ms-playwright",
        }.items():
            raw[key] = str(value)
        config = load_runtime_config(raw, checkout_root=checkout)
        self.assertTrue(is_within(config.archive_root, checkout / "_MandarinGallery"))
        for path in (config.state_path, config.temp_root, config.log_root, config.browser_cache_path):
            assert path is not None
            self.assertFalse(is_within(path, checkout))


    def test_preflight_creates_only_runtime_parents_and_requires_archive(self) -> None:
        config = load_runtime_config(self.raw(), checkout_root=Path.cwd())
        with self.assertRaises(ConfigError):
            config.preflight()
        config.archive_root.mkdir()
        config.preflight()
        self.assertTrue(config.state_path.parent.is_dir())
        self.assertTrue(config.temp_root.is_dir())
        self.assertTrue(config.log_root.is_dir())


    def test_account_identity_is_required_and_non_empty_string(self) -> None:
        for value in (None, "", "   ", 123, ["SYNTHETIC-INTENDED-ACCOUNT"]):
            raw = self.raw()
            raw["account_identity"] = value
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_runtime_config(raw, checkout_root=Path.cwd())

    def test_with_overrides_preserves_account_identity(self) -> None:
        config = load_runtime_config(self.raw(), checkout_root=Path.cwd())
        overridden = config.with_overrides({"timeout_seconds": 10})
        self.assertEqual(overridden.account_identity, "SYNTHETIC-INTENDED-ACCOUNT")

    def test_example_config_uses_only_synthetic_account_identity(self) -> None:
        example = Path(__file__).parents[1] / "config" / "energygrid.example.json"
        raw = json.loads(example.read_text(encoding="utf-8"))
        self.assertEqual(raw["account_identity"], "REPLACE_WITH_PRIVATE_ACCOUNT_IDENTITY")
        self.assertNotIn("C&W", raw["account_identity"])

if __name__ == "__main__":
    unittest.main()
