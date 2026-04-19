from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app import main as app_main  # noqa: E402
from app import migrate as app_migrate  # noqa: E402


class FakeCursor:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self._rows = rows or []

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, mapping: dict[str, list[dict[str, object]]]) -> None:
        self.mapping = mapping
        self.closed = False

    def execute(self, query: str, params: tuple[object, ...] | list[object] = ()) -> FakeCursor:
        normalized = " ".join(query.split())
        for prefix, rows in self.mapping.items():
            if normalized.startswith(prefix):
                return FakeCursor(rows)
        raise AssertionError(f"Unexpected query: {normalized} params={params!r}")

    def close(self) -> None:
        self.closed = True


class RuntimeValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_settings = app_main.settings

    def tearDown(self) -> None:
        app_main.settings = self.original_settings

    def test_validate_runtime_config_allows_minimal_dry_run(self) -> None:
        app_main.settings = replace(
            app_main.settings,
            database_url="postgresql://example",
            dispatch_mode="dry_run",
            notify_target="",
            telegram_api_id_raw="",
            telegram_api_hash="",
            telegram_session_string="",
            smtp_host="",
            smtp_username="",
            smtp_password="",
            smtp_from_email="",
        )

        app_main.validate_runtime_config()

    def test_validate_runtime_config_rejects_manual_send_without_delivery_runtime(self) -> None:
        app_main.settings = replace(
            app_main.settings,
            database_url="postgresql://example",
            dispatch_mode="manual_send",
            notify_target="",
            telegram_api_id_raw="",
            telegram_api_hash="",
            telegram_session_string="",
            smtp_host="",
            smtp_username="",
            smtp_password="",
            smtp_from_email="",
        )

        with self.assertRaises(app_main.RuntimeValidationError) as ctx:
            app_main.validate_runtime_config()

        self.assertIn("manual_send requires Telegram runtime credentials or SMTP runtime configuration", str(ctx.exception))

    def test_validate_runtime_config_rejects_notify_target_without_telegram(self) -> None:
        app_main.settings = replace(
            app_main.settings,
            database_url="postgresql://example",
            dispatch_mode="dry_run",
            notify_target="me",
            telegram_api_id_raw="",
            telegram_api_hash="",
            telegram_session_string="",
        )

        with self.assertRaises(app_main.RuntimeValidationError) as ctx:
            app_main.validate_runtime_config()

        self.assertIn("TG_OUTREACH_NOTIFY_TARGET requires Telegram runtime credentials", str(ctx.exception))


class MigrateCommandTests(unittest.TestCase):
    def test_migrate_command_emits_applied_versions(self) -> None:
        fake_connection = FakeConnection({})
        captured = io.StringIO()
        with (
            patch.object(app_migrate, "get_db", return_value=fake_connection),
            patch.object(app_migrate, "get_applied_migrations", return_value=["001_init.sql", "002_extra.sql"]),
            patch.object(app_migrate, "database_backend_name", return_value="postgres"),
            redirect_stdout(captured),
        ):
            app_migrate.main()

        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["database_backend"], "postgres")
        self.assertEqual(payload["applied_count"], 2)
        self.assertEqual(payload["applied_versions"], ["001_init.sql", "002_extra.sql"])
        self.assertTrue(fake_connection.closed)


class OpsSummaryTests(unittest.TestCase):
    def test_ops_summary_reports_dependency_degradation(self) -> None:
        fake_connection = FakeConnection(
            {
                "SELECT * FROM jobs": [
                    {
                        "status": "pending",
                        "job_type": "telegram_reply_poll",
                        "run_at": "2026-04-20T00:00:00+00:00",
                        "lease_expires_at": None,
                        "created_at": "job-created",
                    }
                ],
                "SELECT * FROM recruiters": [
                    {
                        "status": "new",
                        "outbound_count": 0,
                        "inbound_count": 0,
                    }
                ],
                "SELECT * FROM conversations": [],
                "SELECT structured_json, draft_source FROM vacancies": [
                    {
                        "structured_json": json.dumps(
                            {
                                "contact_extraction_status": "resolved",
                                "contact_extraction_reason": "explicit_input",
                            }
                        ),
                        "draft_source": "fallback:http_error:HTTPStatusError",
                    }
                ],
            }
        )

        with (
            patch.object(app_main, "get_db", return_value=fake_connection),
            patch.object(app_main, "get_control_state_value", return_value=({"worker_id": "worker-1"}, "heartbeat-ts")),
            patch.object(app_main, "age_seconds_from_iso", side_effect=lambda value: 10 if value == "heartbeat-ts" else 20),
            patch.object(app_main, "probe_astrixa_health", return_value={"status": "ok", "latency_ms": 15}),
            patch.object(app_main, "probe_astrixa_invoke", return_value={"status": "error", "latency_ms": 650}),
        ):
            summary = app_main.ops_summary()

        self.assertEqual(summary.worker_status, "ok")
        self.assertEqual(summary.worker_heartbeat_age_seconds, 10)
        self.assertEqual(summary.astrixa_health_status, "ok")
        self.assertEqual(summary.astrixa_invoke_status, "error")
        self.assertTrue(summary.dependency_degraded)
        self.assertEqual(summary.generation_sources["fallback:http_error:HTTPStatusError"], 1)
        self.assertEqual(summary.fallback_generations, 1)
        self.assertTrue(fake_connection.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
