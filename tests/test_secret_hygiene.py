from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_secret_hygiene


class SecretHygieneTests(unittest.TestCase):
    def test_main_passes_when_tracked_files_are_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            clean_file = repo_root / "README.md"
            clean_file.write_text("clean content\n", encoding="utf-8")

            with (
                patch.object(check_secret_hygiene, "REPO_ROOT", repo_root),
                patch.object(check_secret_hygiene, "git_tracked_files", return_value=["README.md"]),
            ):
                result = check_secret_hygiene.main()

        self.assertEqual(result, 0)

    def test_main_fails_on_tracked_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            env_file = repo_root / ".env"
            env_file.write_text("DUMMY=1\n", encoding="utf-8")

            with (
                patch.object(check_secret_hygiene, "REPO_ROOT", repo_root),
                patch.object(check_secret_hygiene, "git_tracked_files", return_value=[".env"]),
            ):
                result = check_secret_hygiene.main()

        self.assertEqual(result, 1)

    def test_main_fails_on_secret_pattern_in_tracked_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            tracked_file = repo_root / "tracked.txt"
            tracked_file.write_text("TG_OUTREACH_SMTP_PASSWORD=secret\n", encoding="utf-8")

            with (
                patch.object(check_secret_hygiene, "REPO_ROOT", repo_root),
                patch.object(check_secret_hygiene, "git_tracked_files", return_value=["tracked.txt"]),
            ):
                result = check_secret_hygiene.main()

        self.assertEqual(result, 1)
