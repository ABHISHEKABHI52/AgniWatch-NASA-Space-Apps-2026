import csv
import io
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import (
    app,
    generate_mock_observations,
    Observation,
)

# Expected values derived from the deterministic mock dataset (seed 42).
MOCK_RECORDS = generate_mock_observations()
MOCK_BY_SENSOR = {
    sensor: sum(1 for r in MOCK_RECORDS if r.source_sensor == sensor) for sensor in ("MODIS", "VIIRS")
}
MOCK_START = min(r.acq_datetime_utc.date() for r in MOCK_RECORDS)
MOCK_END = max(r.acq_datetime_utc.date() for r in MOCK_RECORDS)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        os.environ["AGNIWATCH_DATA_MODE"] = "DEMO"

    def test_health(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["data_status"], "DEMO")

    def test_metadata(self):
        response = self.client.get("/api/metadata")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "DEMO")
        self.assertTrue(body["is_demo"])
        self.assertIn("MODIS demo fixture", body["products"])
        self.assertIn("VIIRS demo fixture", body["products"])

    def test_summary_sensor_filtering(self):
        response = self.client.get(f"/api/summary?sensors=MODIS")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["by_sensor"]["MODIS"], MOCK_BY_SENSOR["MODIS"])
        self.assertEqual(body["by_sensor"]["VIIRS"], 0)
        self.assertEqual(body["total_records"], MOCK_BY_SENSOR["MODIS"])

    def test_summary_all_sensors(self):
        response = self.client.get("/api/summary")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_records"], len(MOCK_RECORDS))
        self.assertEqual(body["by_sensor"]["MODIS"], MOCK_BY_SENSOR["MODIS"])
        self.assertEqual(body["by_sensor"]["VIIRS"], MOCK_BY_SENSOR["VIIRS"])
        self.assertEqual(body["coverage"]["start"], MOCK_START.isoformat())
        self.assertEqual(body["coverage"]["end"], MOCK_END.isoformat())

    def test_invalid_date_range(self):
        response = self.client.get("/api/observations?start=2025-10-29&end=2025-08-01")
        self.assertEqual(response.status_code, 422)

    def test_missing_values_are_preserved(self):
        records = self.client.get("/api/observations").json()["records"]
        null_frp = [r for r in records if r.get("frp") is None]
        self.assertGreater(len(null_frp), 0, "expected at least one record with null frp")
        self.assertIsNone(null_frp[0]["frp"])

    def test_empty_sensor_selection(self):
        response = self.client.get("/api/summary?sensors=__NONE__")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_records"], 0)
        calendar = self.client.get("/api/activity-calendar?sensors=__NONE__").json()["days"]
        self.assertEqual(calendar, {})

    def test_date_filtering(self):
        first_day = MOCK_START.isoformat()
        records = self.client.get(f"/api/observations?start={first_day}&end={first_day}").json()["records"]
        for record in records:
            self.assertTrue(record["acq_datetime_utc"].startswith(first_day))

    def test_trends_covers_requested_range(self):
        start = MOCK_START
        end = start + timedelta(days=3)
        params = f"?start={start.isoformat()}&end={end.isoformat()}"
        points = self.client.get("/api/trends" + params).json()["points"]
        self.assertEqual([p["date"] for p in points], [(start + timedelta(days=offset)).isoformat() for offset in range(4)])
        response = self.client.get("/api/observations" + params)
        self.assertEqual(sum(p["MODIS"] + p["VIIRS"] for p in points), len(response.json()["records"]))

    def test_coordinate_validity_on_mock_records(self):
        records = self.client.get("/api/observations").json()["records"]
        self.assertGreater(len(records), 0)
        for record in records:
            self.assertGreaterEqual(record["latitude"], -90)
            self.assertLessEqual(record["latitude"], 90)
            self.assertGreaterEqual(record["longitude"], -180)
            self.assertLessEqual(record["longitude"], 180)

    def test_area_filtering(self):
        all_count = self.client.get("/api/summary").json()["total_records"]
        surat_count = self.client.get("/api/summary?area=surat").json()["total_records"]
        self.assertGreater(surat_count, 0)
        self.assertLessEqual(surat_count, all_count)

    def test_model_rejects_out_of_range_coordinates(self):
        with self.assertRaises(Exception):
            Observation(
                observation_id="bad-0001",
                record_id="bad-0001",
                source_sensor="MODIS",
                product="MOD14A1 demo fixture",
                acq_datetime_utc=datetime(2025, 8, 1, 4, 20, tzinfo=timezone.utc),
                latitude=91.0,
                longitude=72.83,
            )

    def test_csv_export_matches_filtered_records(self):
        params = "?start=" + MOCK_START.isoformat() + "&end=" + MOCK_END.isoformat() + "&sensors=MODIS&sensors=VIIRS"
        expected = self.client.get("/api/observations" + params).json()["records"]
        response = self.client.get("/api/export" + params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        reader = csv.DictReader(io.StringIO(response.text))
        rows = list(reader)
        self.assertEqual(len(rows), len(expected))
        self.assertEqual(rows[0]["observation_id"], expected[0]["observation_id"])
        self.assertIn("observation_id", response.text)
        self.assertTrue(all(row["is_demo"] == "True" for row in rows))


if __name__ == "__main__":
    unittest.main()