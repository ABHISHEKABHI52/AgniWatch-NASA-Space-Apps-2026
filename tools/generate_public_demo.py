from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


FIELDS = (
    "record_id", "sensor", "platform", "product", "acquired",
    "latitude", "longitude", "confidence", "brightness", "frp",
    "area", "quality", "source",
)


def _row_to_record(row: sqlite3.Row) -> Dict[str, Any]:
    acquired = row["acquired"]
    return {
        "observation_id": row["record_id"],
        "record_id": row["record_id"],
        "source_sensor": row["sensor"],
        "source_platform": row["platform"],
        "product": row["product"],
        "acq_datetime_utc": acquired,
        "acquisition_datetime_utc": acquired,
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "confidence": row["confidence_number"] if row["confidence_number"] is not None else row["confidence"],
        "brightness": row["brightness"],
        "frp": row["frp"],
        "area_of_interest": row["area"],
        "quality_flag": row["quality"],
        "source": row["source"],
        "is_demo": False,
    }


def _sample_sensor(
    connection: sqlite3.Connection,
    sensor: str,
    count: int,
    start_rowid: int,
    total_rows: int,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for index in range(count):
        low = start_rowid + (index * total_rows // count)
        high = start_rowid + (((index + 1) * total_rows) // count) - 1
        row = connection.execute(
            "SELECT record_id,sensor,platform,product,acquired,latitude,longitude,"
            "confidence,confidence_number,brightness,frp,area,quality,source "
            "FROM observations WHERE sensor=? AND rowid BETWEEN ? AND ? ORDER BY rowid LIMIT 1",
            (sensor, low, max(low, high)),
        ).fetchone()
        if row:
            rows.append(_row_to_record(row))
    return rows


def _summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_sensor = Counter(record["source_sensor"] for record in records)
    dates = [record["acq_datetime_utc"][:10] for record in records]
    frp = [record["frp"] for record in records if isinstance(record["frp"], (int, float))]
    return {
        "total_records": len(records),
        "by_sensor": {sensor: by_sensor.get(sensor, 0) for sensor in ("MODIS", "VIIRS")},
        "coverage": {"start": min(dates) if dates else None, "end": max(dates) if dates else None},
        "coordinates": {
            "west": min((record["longitude"] for record in records), default=None),
            "east": max((record["longitude"] for record in records), default=None),
            "south": min((record["latitude"] for record in records), default=None),
            "north": max((record["latitude"] for record in records), default=None),
        },
        "frp": {
            "available": bool(frp),
            "min": min(frp) if frp else None,
            "max": max(frp) if frp else None,
        },
    }


def generate(database: Path, output: Path, per_sensor: int) -> None:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        progress = {
            row["sensor"]: row["rows"]
            for row in connection.execute(
                "SELECT sensor,SUM(inserted_records) AS rows FROM file_progress GROUP BY sensor"
            )
        }
        modis_rows = int(progress.get("MODIS", 0))
        viirs_rows = int(progress.get("VIIRS", 0))
        records = _sample_sensor(connection, "MODIS", per_sensor, 1, modis_rows)
        records.extend(_sample_sensor(connection, "VIIRS", per_sensor, modis_rows + 1, viirs_rows))
    finally:
        connection.close()

    records.sort(key=lambda record: (record["acq_datetime_utc"], record["record_id"]))
    payload = {
        "mode": "PUBLIC_DEMO",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "provenance": {
            "source": "NASA MODIS and VIIRS active-fire observations",
            "selection": "Deterministic evenly spaced rowid sample from the committed local SQLite index",
            "query_method": "Read-only rowid ranges; ORDER BY RANDOM() was not used",
            "sample_is_complete_dataset": False,
            "committed_index_counts": {"MODIS": modis_rows, "VIIRS": viirs_rows},
            "index_ready": False,
        },
        "summary": _summary(records),
        "observations": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")
    print(json.dumps({"output": str(output), "records": len(records), "summary": payload["summary"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a public NASA sample without modifying SQLite.")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-sensor", type=int, default=2000)
    args = parser.parse_args()
    if args.per_sensor < 1:
        parser.error("--per-sensor must be positive")
    generate(args.database, args.output, args.per_sensor)
