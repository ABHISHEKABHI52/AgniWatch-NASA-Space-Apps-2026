import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.local_data import LocalDatasetProvider, set_observation_model
from app.main import Observation


class LocalDatasetTests(unittest.TestCase):
    def setUp(self):
        set_observation_model(Observation)

    def test_normalizes_modis_and_viirs_source_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modis = root / "modis" / "2025"
            viirs = root / "viirs" / "2025"
            modis.mkdir(parents=True)
            viirs.mkdir(parents=True)
            self._write(modis / "modis_2025_Testland.csv", [
                ["latitude", "longitude", "brightness", "acq_date", "acq_time", "satellite", "confidence", "frp", "daynight"],
                ["21.1", "72.8", "310.2", "2025-01-02", "420", "Terra", "82", "18.4", "D"],
            ])
            self._write(viirs / "viirs_2025_Testland.csv", [
                ["latitude", "longitude", "bright_ti4", "acq_date", "acq_time", "satellite", "confidence", "frp", "daynight"],
                ["21.2", "72.9", "332.0", "2025-01-03", "1805", "N", "n", "8.6", "N"],
            ])

            records = LocalDatasetProvider(root, max_records=10).all_records()

            self.assertEqual({record.source_sensor for record in records}, {"MODIS", "VIIRS"})
            modis_record = next(record for record in records if record.source_sensor == "MODIS")
            viirs_record = next(record for record in records if record.source_sensor == "VIIRS")
            self.assertEqual(modis_record.brightness, 310.2)
            self.assertEqual(viirs_record.source_platform, "Suomi-NPP")
            self.assertEqual(viirs_record.confidence, "n")
            self.assertFalse(modis_record.is_demo)

    def test_rejects_invalid_coordinates_without_synthetic_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modis = root / "modis"
            modis.mkdir(parents=True)
            self._write(modis / "bad.csv", [
                ["latitude", "longitude", "acq_date", "acq_time"],
                ["999", "72.8", "2025-01-02", "420"],
            ])

            provider = LocalDatasetProvider(root, max_records=10)
            self.assertEqual(provider.all_records(), [])
            self.assertEqual(provider.metadata()["datasets"]["MODIS"]["invalid_coordinates"], 1)

    def test_queries_complete_index_in_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modis = root / "modis"
            modis.mkdir(parents=True)
            self._write(modis / "records.csv", [
                ["latitude", "longitude", "brightness", "acq_date", "acq_time", "satellite", "confidence", "frp", "daynight"],
                ["21.1", "72.8", "310", "2025-01-01", "0100", "Terra", "82", "2", "D"],
                ["21.2", "72.9", "311", "2025-01-02", "0100", "Terra", "83", "3", "D"],
                ["21.3", "73.0", "312", "2025-01-03", "0100", "Terra", "84", "4", "D"],
            ])

            provider = LocalDatasetProvider(root, max_records=1, index_path=root / "index.sqlite")
            first, total = provider.query_records(limit=2, offset=0)
            second, second_total = provider.query_records(limit=2, offset=2)

            self.assertEqual(total, 3)
            self.assertEqual(second_total, 3)
            self.assertEqual(len(first), 2)
            self.assertEqual(len(second), 1)
            self.assertIsNone(provider.metadata()["record_cap"])

    def test_interrupted_file_rolls_back_without_progress_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modis = root / "modis"
            modis.mkdir(parents=True)
            self._write(modis / "records.csv", [
                ["latitude", "longitude", "brightness", "acq_date", "acq_time"],
                ["21.1", "72.8", "310", "2025-01-01", "0100"],
            ])

            class InterruptedProvider(LocalDatasetProvider):
                def _iter_file_rows(self, path):
                    yield 2, {
                        "latitude": "21.1", "longitude": "72.8",
                        "brightness": "310", "acq_date": "2025-01-01", "acq_time": "0100",
                    }
                    raise KeyboardInterrupt

            index_path = root / "index.sqlite"
            provider = InterruptedProvider(root, index_path=index_path)
            with self.assertRaises(KeyboardInterrupt):
                provider.all_records()

            connection = sqlite3.connect(index_path)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM file_progress").fetchone()[0], 0)
            connection.close()

    @staticmethod
    def _write(path, rows):
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(rows)


if __name__ == "__main__":
    unittest.main()
