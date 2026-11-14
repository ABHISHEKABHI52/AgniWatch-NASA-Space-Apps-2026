from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOGGER = logging.getLogger("agniwatch.local_data")
ObservationModel: Any = None


def set_observation_model(model: Any) -> None:
    global ObservationModel
    ObservationModel = model


_SENSOR_CONFIG = {
    "MODIS": {"directory": "modis", "brightness_field": "brightness", "platforms": {}, "product": "MODIS active-fire CSV"},
    "VIIRS": {
        "directory": "viirs",
        "brightness_field": "bright_ti4",
        "platforms": {"N": "Suomi-NPP", "J": "NOAA-20", "N20": "NOAA-20"},
        "product": "VIIRS SNPP active-fire CSV",
    },
}


def _number(value: Optional[str]) -> Optional[float]:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _confidence(value: Optional[str]) -> Optional[float | str]:
    if value is None or value.strip() == "":
        return None
    numeric = _number(value)
    return numeric if numeric is not None else value.strip()


def _timestamp(row: Dict[str, str]) -> datetime:
    raw_date = (row.get("acq_date") or "").strip()
    raw_time = (row.get("acq_time") or "").strip().zfill(4)
    return datetime.strptime(f"{raw_date} {raw_time}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)


def _record_id(sensor: str, path: Path, row_number: int, row: Dict[str, str]) -> str:
    identity = f"{sensor}:{path.as_posix()}:{row_number}:{row.get('acq_date')}:{row.get('acq_time')}:{row.get('latitude')}:{row.get('longitude')}"
    return f"{sensor.lower()}-local-{hashlib.sha1(identity.encode('utf-8')).hexdigest()}"


class LocalDatasetProvider:
    """Indexes local NASA CSV files in SQLite and queries normalized rows on demand."""

    def __init__(
        self,
        data_root: Path,
        max_records: int = 50_000,
        index_path: Optional[Path] = None,
        allow_partial: bool = False,
    ) -> None:
        self.data_root = data_root
        self.max_records = max(1, max_records)
        self.index_path = index_path or Path(os.getenv("AGNIWATCH_LOCAL_INDEX", str(data_root.parent / "backend" / "data" / "agniwatch_local.sqlite3")))
        self.allow_partial = allow_partial
        self._lock = Lock()
        self._files: Dict[str, List[Path]] = {}
        self._warnings: List[str] = []
        self._stats: Dict[str, Dict[str, Any]] = {}
        self._index_files()

    def _index_files(self) -> None:
        for sensor, config in _SENSOR_CONFIG.items():
            directory = self.data_root / config["directory"]
            files = sorted(directory.rglob("*.csv")) if directory.is_dir() else []
            self._files[sensor] = files
            self._stats[sensor] = {
                "directory": str(directory), "files": len(files), "raw_records": 0,
                "records_loaded": 0, "valid_records": 0, "malformed_records": 0,
                "invalid_coordinates": 0, "missing_timestamps": 0, "duplicates": 0,
                "date_start": None, "date_end": None,
            }
            if not files:
                self._warnings.append(f"{sensor} dataset directory is missing or contains no CSV files: {directory}")

    def status(self) -> str:
        if not any(self._files.values()):
            return "ERROR"
        return "PARTIAL" if self.allow_partial else "LOCAL"

    def _manifest(self) -> str:
        digest = hashlib.sha256()
        for sensor in ("MODIS", "VIIRS"):
            for path in self._files[sensor]:
                stat = path.stat()
                digest.update(f"{sensor}|{path.relative_to(self.data_root)}|{stat.st_size}|{stat.st_mtime_ns}".encode())
        return digest.hexdigest()

    def _connect(self) -> sqlite3.Connection:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.index_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-262144")
        return connection

    def _ensure_index(self) -> None:
        with self._lock:
            manifest = self._manifest()
            connection = self._connect()
            try:
                connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS file_progress (sensor TEXT NOT NULL, path TEXT NOT NULL, raw_records INTEGER NOT NULL, valid_records INTEGER NOT NULL, inserted_records INTEGER NOT NULL, PRIMARY KEY(sensor, path))"
                )
                current = connection.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
                ready = connection.execute("SELECT value FROM metadata WHERE key='ready'").fetchone()
                if current and ready and current["value"] == manifest and ready["value"] == "1":
                    self._load_stats(connection)
                    return
                if self.allow_partial and current and current["value"] != manifest:
                    raise RuntimeError("Partial mode index manifest does not match the source files; refusing to rebuild the database.")
                if not current or current["value"] != manifest:
                    if self.allow_partial:
                        raise RuntimeError("Partial mode requires an existing SQLite index.")
                    connection.execute("DROP TABLE IF EXISTS observations")
                    connection.execute("DELETE FROM file_progress")
                    connection.execute("DELETE FROM metadata WHERE key IN ('stats', 'ready')")
                    connection.execute(
                        """CREATE TABLE observations (
                            record_id TEXT PRIMARY KEY, sensor TEXT NOT NULL, platform TEXT, product TEXT NOT NULL,
                            acquired TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
                            confidence TEXT, confidence_number REAL, brightness REAL, frp REAL,
                            area TEXT, quality TEXT, source TEXT NOT NULL
                        )"""
                    )
                    connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('manifest',?),('ready','0')", (manifest,))
                    connection.commit()
                if current and current["value"] == manifest:
                    self._load_stats(connection)
                    if self.allow_partial:
                        return
                self._scan_into_index(connection)
                connection.execute("CREATE INDEX IF NOT EXISTS idx_observations_sensor_date ON observations(sensor, acquired)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_observations_area ON observations(area)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_observations_coordinates ON observations(latitude, longitude)")
                connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('stats',?),('ready','1')", (json.dumps(self._stats),))
                connection.commit()
            finally:
                connection.close()

    def _scan_into_index(self, connection: sqlite3.Connection) -> None:
        for sensor in ("MODIS", "VIIRS"):
            config = _SENSOR_CONFIG[sensor]
            for path in self._files[sensor]:
                path_key = str(path.relative_to(self.data_root))
                if connection.execute("SELECT 1 FROM file_progress WHERE sensor=? AND path=?", (sensor, path_key)).fetchone():
                    continue
                stats = {
                    "directory": self._stats[sensor]["directory"], "files": self._stats[sensor]["files"],
                    "raw_records": 0, "records_loaded": 0, "valid_records": 0,
                    "malformed_records": 0, "invalid_coordinates": 0, "missing_timestamps": 0,
                    "duplicates": 0, "date_start": None, "date_end": None,
                }
                batch = []
                try:
                    connection.execute("BEGIN")
                    for row_number, row in self._iter_file_rows(path):
                            stats["raw_records"] += 1
                            try:
                                latitude = float(row["latitude"])
                                longitude = float(row["longitude"])
                                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                                    stats["invalid_coordinates"] += 1
                                    continue
                                acquired = _timestamp(row)
                                if not row.get("acq_date") or not row.get("acq_time"):
                                    stats["missing_timestamps"] += 1
                                    continue
                                record_id = _record_id(sensor, path, row_number, row)
                                confidence = _confidence(row.get("confidence"))
                                current = acquired.date().isoformat()
                                stats["valid_records"] += 1
                                stats["date_start"] = min(stats["date_start"] or current, current)
                                stats["date_end"] = max(stats["date_end"] or current, current)
                                batch.append((
                                    record_id, sensor, config["platforms"].get((row.get("satellite") or "").strip(), (row.get("satellite") or "").strip() or None),
                                    config["product"], acquired.isoformat(), latitude, longitude,
                                    None if confidence is None else str(confidence),
                                    confidence if isinstance(confidence, (int, float)) else None,
                                    _number(row.get(config["brightness_field"])), _number(row.get("frp")),
                                    path.stem.rsplit("_", 1)[-1], (row.get("daynight") or "").strip() or None,
                                    f"NASA local dataset: {path.name}",
                                ))
                                if len(batch) >= 100_000:
                                    before = connection.total_changes
                                    connection.executemany("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                                    inserted = connection.total_changes - before
                                    stats["records_loaded"] += inserted
                                    stats["duplicates"] += len(batch) - inserted
                                    batch.clear()
                            except (KeyError, TypeError, ValueError):
                                stats["malformed_records"] += 1
                except (OSError, UnicodeError, csv.Error) as error:
                    warning = f"Unable to read {path.name}: {error}"
                    LOGGER.warning(warning)
                    self._warnings.append(warning)
                    connection.rollback()
                    continue
                except BaseException:
                    connection.rollback()
                    raise
                try:
                    if batch:
                        before = connection.total_changes
                        connection.executemany("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                        inserted = connection.total_changes - before
                        stats["records_loaded"] += inserted
                        stats["duplicates"] += len(batch) - inserted
                    connection.execute(
                        "INSERT INTO file_progress(sensor,path,raw_records,valid_records,inserted_records) VALUES (?,?,?,?,?)",
                        (sensor, path_key, stats["raw_records"], stats["valid_records"], stats["records_loaded"]),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                self._merge_file_stats(sensor, stats)
                connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('stats',?)", (json.dumps(self._stats),))
                connection.commit()

    def _iter_file_rows(self, path: Path) -> Iterable[Tuple[int, Dict[str, str]]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), 2):
                yield row_number, row

    def _merge_file_stats(self, sensor: str, file_stats: Dict[str, Any]) -> None:
        stats = self._stats[sensor]
        for key in ("raw_records", "records_loaded", "valid_records", "malformed_records", "invalid_coordinates", "missing_timestamps", "duplicates"):
            stats[key] += file_stats[key]
        for key in ("date_start", "date_end"):
            value = file_stats[key]
            if value and (stats[key] is None or (key == "date_start" and value < stats[key]) or (key == "date_end" and value > stats[key])):
                stats[key] = value

    def _load_stats(self, connection: sqlite3.Connection) -> None:
        row = connection.execute("SELECT value FROM metadata WHERE key='stats'").fetchone()
        if row:
            self._stats = json.loads(row["value"])

    @staticmethod
    def _where(start=None, end=None, sensors=None, area=None, west=None, south=None, east=None, north=None):
        clauses, params = [], []
        if start: clauses.append("date(acquired) >= date(?)"); params.append(start.isoformat())
        if end: clauses.append("date(acquired) <= date(?)"); params.append(end.isoformat())
        selected = [str(sensor).upper() for sensor in (sensors or [])]
        if selected:
            clauses.append("sensor IN (" + ",".join("?" for _ in selected) + ")"); params.extend(selected)
        if area and area.lower() != "all": clauses.append("lower(area)=lower(?)"); params.append(area)
        for expression, value in (("longitude >= ?", west), ("latitude >= ?", south), ("longitude <= ?", east), ("latitude <= ?", north)):
            if value is not None: clauses.append(expression); params.append(value)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _row_to_observation(self, row: sqlite3.Row) -> Any:
        return ObservationModel(
            observation_id=row["record_id"], record_id=row["record_id"], source_sensor=row["sensor"],
            source_platform=row["platform"], product=row["product"],
            acq_datetime_utc=datetime.fromisoformat(row["acquired"]),
            acquisition_datetime_utc=datetime.fromisoformat(row["acquired"]),
            latitude=row["latitude"], longitude=row["longitude"], confidence=_confidence(row["confidence"]),
            brightness=row["brightness"], frp=row["frp"], area_of_interest=row["area"],
            quality_flag=row["quality"], source=row["source"], is_demo=False,
        )

    def query_records(self, start=None, end=None, sensors=None, area=None, west=None, south=None, east=None, north=None, offset=0, limit=5000) -> Tuple[List[Any], int]:
        self._ensure_index()
        where, params = self._where(start, end, sensors, area, west, south, east, north)
        connection = self._connect()
        try:
            selected = {str(sensor).upper() for sensor in (sensors or [])}
            unfiltered = not start and not end and (not area or area.lower() == "all") and all(
                value is None for value in (west, south, east, north)
            )
            if self.allow_partial and unfiltered:
                total = sum(
                    self._stats[sensor]["records_loaded"]
                    for sensor in (selected or {"MODIS", "VIIRS"})
                    if sensor in self._stats
                )
                order = "rowid DESC"
            else:
                total = connection.execute("SELECT COUNT(*) AS count FROM observations" + where, params).fetchone()["count"]
                order = "acquired DESC"
            rows = connection.execute("SELECT * FROM observations" + where + f" ORDER BY {order} LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        finally:
            connection.close()
        return [self._row_to_observation(row) for row in rows], total

    def all_records(self) -> List[Any]:
        records, _ = self.query_records(limit=self.max_records)
        return records

    def summary(self, **filters) -> Dict[str, Any]:
        self._ensure_index()
        where, params = self._where(**filters)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS total, MIN(date(acquired)) AS start, MAX(date(acquired)) AS end, "
                "SUM(sensor='MODIS') AS modis, SUM(sensor='VIIRS') AS viirs, "
                "MIN(frp) AS frp_min, MAX(frp) AS frp_max, COUNT(confidence_number) AS confidence_numeric FROM observations" + where, params
            ).fetchone()
        finally:
            connection.close()
        return {"total": row["total"], "modis": row["modis"] or 0, "viirs": row["viirs"] or 0, "start": row["start"], "end": row["end"], "frp_min": row["frp_min"], "frp_max": row["frp_max"], "confidence_numeric": row["confidence_numeric"]}

    def grouped_counts(self, grouping: str, **filters) -> Dict[str, Dict[str, int]]:
        self._ensure_index()
        expression = "date(acquired)" if grouping == "day" else "strftime('%Y-%W', acquired)" if grouping == "week" else "strftime('%Y-%m', acquired)"
        where, params = self._where(**filters)
        connection = self._connect()
        try:
            rows = connection.execute(f"SELECT {expression} AS bucket, sensor, COUNT(*) AS count FROM observations{where} GROUP BY bucket, sensor ORDER BY bucket", params).fetchall()
        finally:
            connection.close()
        result: Dict[str, Dict[str, int]] = {}
        for row in rows:
            result.setdefault(row["bucket"], {"MODIS": 0, "VIIRS": 0})[row["sensor"]] = row["count"]
        return result

    def metadata(self) -> Dict[str, Any]:
        self._ensure_index()
        dates = [stats["date_start"] for stats in self._stats.values() if stats["date_start"]] + [stats["date_end"] for stats in self._stats.values() if stats["date_end"]]
        return {
            "status": self.status(), "source": "NASA MODIS and VIIRS local CSV datasets",
            "products": ["MODIS", "VIIRS SNPP"], "spatial_grid": "Source coordinates; no spatial deduplication",
            "time_bin": "UTC acquisition timestamp", "retrieved_at": None, "freshness": "local",
            "warnings": list(dict.fromkeys(self._warnings)), "is_demo": False,
            "area": {"west": -180, "south": -90, "east": 180, "north": 90},
            "datasets": self._stats, "records_loaded": sum(s["records_loaded"] for s in self._stats.values()),
            "date_range": {"start": min(dates) if dates else None, "end": max(dates) if dates else None},
            "data_root": str(self.data_root), "record_cap": None, "configured_record_cap": self.max_records,
            "index_path": str(self.index_path),
            "index_ready": self._index_ready(),
            "index_state": "partial" if self.allow_partial and not self._index_ready() else "complete",
            "note": "NASA data — partially indexed. Only committed records are served; indexing can be resumed separately. Partial-mode pagination uses committed file totals and insertion order until final indexes are built." if self.allow_partial else "The valid dataset is indexed on disk when index_ready is true; API responses are paginated and do not load the archive into RAM.",
        }

    def _index_ready(self) -> bool:
        if not self.index_path.exists():
            return False
        connection = self._connect()
        try:
            row = connection.execute("SELECT value FROM metadata WHERE key='ready'").fetchone()
            return bool(row and row["value"] == "1")
        finally:
            connection.close()
