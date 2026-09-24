"""Тестовые данные не подменяют настоящие (общий бэклог №31).

Раньше без APP_DATA_MODE сервер молча отдавал сгенерированные показатели.
Теперь по умолчанию — local, mock только явно, а на рабочем сервере
(APP_STAGE=production) запросы показателей с mock получают 503.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from backend import main


class DataModeTests(unittest.TestCase):
    def env(self, **values):
        clean = {k: v for k, v in os.environ.items() if k not in {"APP_DATA_MODE", "KPI_DATA_MODE", "APP_STAGE"}}
        clean.update(values)
        return mock.patch.dict(os.environ, clean, clear=True)

    def test_default_is_real_data_not_mock(self):
        with self.env():
            self.assertEqual(main.data_mode(), "local")
            self.assertFalse(main.mock_blocked())

    def test_mock_is_explicit_and_allowed_only_outside_production(self):
        with self.env(APP_DATA_MODE="mock"):
            self.assertEqual(main.kpi_mode(), "mock")
        with self.env(APP_DATA_MODE="mock", APP_STAGE="production"):
            self.assertTrue(main.mock_blocked())
            with self.assertRaises(main.HTTPException) as caught:
                main.kpi_mode()
            self.assertEqual(caught.exception.status_code, 503)

    def test_kpi_endpoint_refuses_mock_on_production(self):
        import pathlib
        import tempfile

        user = main.AuthUser(id=1, email="admin@example.com", isAdmin=True, unrestricted=True)
        main.app.dependency_overrides[main.require_user] = lambda: user
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kpi_db = mock.patch.object(main, "KPI_DB_PATH", pathlib.Path(tmp.name) / "kpi.sqlite3")   # не трогать рабочую базу
        kpi_db.start()
        self.addCleanup(kpi_db.stop)
        try:
            client = TestClient(main.app)
            with self.env(APP_DATA_MODE="mock", APP_STAGE="production"):
                response = client.get("/api/kpis/periods")
            self.assertEqual(response.status_code, 503)
            self.assertIn("APP_DATA_MODE=local", response.json()["detail"])
            with self.env(APP_DATA_MODE="mock"):
                self.assertEqual(client.get("/api/kpis/periods").json()["source"], "mock")
        finally:
            main.app.dependency_overrides.pop(main.require_user, None)


if __name__ == "__main__":
    unittest.main()
