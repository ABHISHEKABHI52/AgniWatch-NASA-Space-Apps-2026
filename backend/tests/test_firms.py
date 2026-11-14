import csv
import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from urllib.error import HTTPError
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from app.main import (
    app,
    _fetch_firms_source,
    _get_firms_source,
    _parse_firms_datetime,
    generate_mock_observations,
    Observation,
)

FIXTURE_CSV = """latitude,longitude,acq_date,acq_time,satellite,confidence,frp,daynight
21.1702,72.8311,2025-05-01,420,Terra,82,18.4,D
21.1820,72.8450,2025-05-02,510,Aqua,76,12.1,N
21.1630,72.8180,2025-05-01,1805,"Suomi-NPP",91,8.6,N
"""


def _make_urlopen(text: str):
    response = MagicMock()
    response.read.return_value = text.encode("utf-8")
    context = MagicMock()
    context.__enter__.return_value = response
    context.__exit__.return_value = False
    return context


class FirmsParsingTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        os.environ["AGNIWATCH_DATA_MODE"] = "DEMO"

    def test_parse_firms_datetime(self):
        row = {"acq_date": "2025-05-01", "acq_time": "420"}
        acquired = _parse_firms_datetime(row)
        self.assertEqual(acquired, datetime(2025, 5, 1, 4, 20, tzinfo=timezone.utc))

    def test_parse_firms_datetime_padded_time(self):
        row = {"acq_date": "2025-05-01", "acq_time": "1805"}
        acquired = _parse_firms_datetime(row)
        self.assertEqual(acquired, datetime(2025, 5, 1, 18, 5, tzinfo=timezone.utc))

    @patch("app.main.urlopen")
    def test_fetch_firms_source_parses_csv_and_keeps_sensor_identity(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen(FIXTURE_CSV)
        os.environ["FIRMS_MAP_KEY"] = "dummy-key"
        modis_records = _fetch_firms_source("MODIS", 2)
        viirs_records = _fetch_firms_source("VIIRS", 2)
        self.assertEqual(len(modis_records), 3)
        self.assertTrue(all(r.source_sensor == "MODIS" for r in modis_records))
        self.assertEqual({r.product for r in modis_records}, {"MODIS_NRT"})
        self.assertEqual(len(viirs_records), 3)
        self.assertTrue(all(r.source_sensor == "VIIRS" for r in viirs_records))
        self.assertEqual({r.product for r in viirs_records}, {"VIIRS_SNPP_NRT"})
        self.assertEqual(modis_records[0].latitude, 21.1702)
        self.assertEqual(modis_records[0].longitude, 72.8311)
        self.assertEqual(modis_records[0].confidence, "82")
        self.assertEqual(modis_records[0].frp, 18.4)
        self.assertIn("Suomi-NPP", [r.source_platform for r in viirs_records])

    @patch("app.main.urlopen")
    def test_fetch_firms_source_rejects_xml_error_response(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen("<Error><Code>InvalidKey</Code></Error>")
        os.environ["FIRMS_MAP_KEY"] = "dummy-key"
        with self.assertRaises(Exception) as ctx:
            _fetch_firms_source("MODIS", 2)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("invalid response", str(ctx.exception.detail))

    @patch("app.main.urlopen")
    def test_fetch_firms_source_auth_failure(self, mock_urlopen):
        error = HTTPError("https://firms.modaps.eosdis.nasa.gov/api/area/csv/x/MODIS_NRT/72.8,21.14,72.88,21.21/1", 403, "Forbidden", {}, None)
        mock_urlopen.side_effect = error
        os.environ["FIRMS_MAP_KEY"] = "dummy-key"
        with self.assertRaises(Exception) as ctx:
            _fetch_firms_source("MODIS", 2)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("authentication", str(ctx.exception.detail).lower())

    @patch("app.main.urlopen")
    def test_fetch_firms_source_rate_limit(self, mock_urlopen):
        error = HTTPError("https://firms.modaps.eosdis.nasa.gov/api/area/csv/x/MODIS_NRT/72.8,21.14,72.88,21.21/1", 429, "Too Many Requests", {}, None)
        mock_urlopen.side_effect = error
        os.environ["FIRMS_MAP_KEY"] = "dummy-key"
        with self.assertRaises(Exception) as ctx:
            _fetch_firms_source("MODIS", 2)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("rate limit", str(ctx.exception.detail).lower())

    @patch("app.main.urlopen")
    def test_fetch_firms_source_network_error(self, mock_urlopen):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        os.environ["FIRMS_MAP_KEY"] = "dummy-key"
        with self.assertRaises(Exception) as ctx:
            _fetch_firms_source("MODIS", 2)
        self.assertEqual(ctx.exception.status_code, 502)

    @patch("app.main.urlopen")
    def test_fetch_firms_source_invalid_coordinate_row(self, mock_urlopen):
        bad_csv = FIXTURE_CSV + "999.0,72.8311,2025-05-01,1200,Terra,50,10,D\n"
        mock_urlopen.return_value = _make_urlopen(bad_csv)
        os.environ["FIRMS_MAP_KEY"] = "dummy-key"
        with self.assertRaises(Exception) as ctx:
            _fetch_firms_source("MODIS", 2)
        self.assertEqual(ctx.exception.status_code, 502)

class FirmsCacheTests(unittest.TestCase):
    def setUp(self):
        import app.main as main_module
        main_module._FIRMS_SOURCE_CACHE.clear()
        main_module._ACTIVE_CACHE = None
        os.environ["AGNIWATCH_DATA_MODE"] = "DEMO"
        os.environ["FIRMS_MAP_KEY"] = "dummy-key"

    @patch("app.main._fetch_firms_source")
    def test_get_firms_source_returns_cached_when_fresh(self, mock_fetch):
        mock_fetch.return_value = [
            Observation(
                observation_id="demo-x",
                record_id="demo-x",
                source_sensor="MODIS",
                source_platform="Terra",
                product="MODIS_NRT",
                acq_datetime_utc=datetime(2025, 8, 1, 4, 20, tzinfo=timezone.utc),
                latitude=21.17,
                longitude=72.83,
            )
        ]
        first = _get_firms_source("MODIS", 2)
        self.assertEqual(first["freshness"], "live")
        second = _get_firms_source("MODIS", 2)
        self.assertEqual(second["freshness"], "cached")
        mock_fetch.assert_called_once()

    @patch("app.main._fetch_firms_source")
    def test_get_firms_source_serves_stale_cache_on_fetch_failure(self, mock_fetch):
        cached = [
            Observation(
                observation_id="demo-cached",
                record_id="demo-cached",
                source_sensor="MODIS",
                source_platform="Terra",
                product="MODIS_NRT",
                acq_datetime_utc=datetime(2025, 8, 1, 4, 20, tzinfo=timezone.utc),
                latitude=21.17,
                longitude=72.83,
            )
        ]
        from fastapi import HTTPException
        mock_fetch.side_effect = HTTPException(502, "NASA FIRMS network request failed for MODIS: boom")
        import app.main as main_module
        main_module._FIRMS_SOURCE_CACHE["MODIS"] = {
            "records": cached,
            "retrieved_at": datetime.now(timezone.utc) - timedelta(seconds=700),
        }
        result = _get_firms_source("MODIS", 2)
        self.assertEqual(result["freshness"], "cached")
        self.assertIn("stale", result["warning"])


class FirmsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        os.environ["AGNIWATCH_DATA_MODE"] = "DEMO"

    def test_metadata_status(self):
        response = self.client.get("/api/metadata")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "DEMO")
        self.assertTrue(body["is_demo"])
        self.assertIsNone(body["freshness"])

    def test_observations_returns_mock_records(self):
        response = self.client.get("/api/observations")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "DEMO")
        self.assertEqual(body["is_demo"], True)
        self.assertGreaterEqual(len(body["records"]), 60)

    def test_observations_filter_by_sensor(self):
        response = self.client.get("/api/observations?sensors=MODIS")
        self.assertEqual(response.status_code, 200)
        records = response.json()["records"]
        self.assertTrue(all(r["source_sensor"] == "MODIS" for r in records))
        self.assertGreater(len(records), 10)

    def test_summary_metrics(self):
        response = self.client.get("/api/summary")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_records"], len(generate_mock_observations()))
        self.assertGreater(body["total_records"], 0)
        self.assertGreater(body["by_sensor"]["MODIS"], 0)
        self.assertGreater(body["by_sensor"]["VIIRS"], 0)
        self.assertIsNotNone(body["coverage"]["start"])
        self.assertIsNotNone(body["coverage"]["end"])

    def test_auth_endpoints(self):
        login = self.client.post("/api/auth/login", json={"username": "demo", "password": "demo"})
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json()["access_token"])
        token = login.json()["access_token"]
        me = self.client.get("/api/auth/me", params={"session_token": token})
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()["authenticated"])
        self.assertEqual(me.json()["user"]["username"], "demo")
        logout = self.client.post("/api/auth/logout", params={"session_token": token})
        self.assertEqual(logout.status_code, 200)
        me_after = self.client.get("/api/auth/me", params={"session_token": token})
        self.assertEqual(me_after.status_code, 401)

    def test_anonymous_auth_is_rejected(self):
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 401)


if __name__ == "__main__":
    unittest.main()