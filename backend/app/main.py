from __future__ import annotations

import csv
import io
import json
import math
import os
import random
import secrets
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .local_data import LocalDatasetProvider, set_observation_model

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_FILE = Path(__file__).parent / "data" / "sample_observations.json"
FIRMS_API = "https://firms.modaps.eosdis.nasa.gov/api/area"
DEFAULT_BBOX = (72.80, 21.14, 72.88, 21.21)  # west, south, east, north - Surat pilot region
FIRMS_SOURCES = {"MODIS": "MODIS_NRT", "VIIRS": "VIIRS_SNPP_NRT"}
FIRMS_MIN_DAYS = 1
FIRMS_MAX_DAYS = 5  # documented area-API limit: 1..5 days per request
ACTIVE_CACHE_TTL_SECONDS = int(os.getenv("AGNIWATCH_ACTIVE_CACHE_TTL", "60"))
FIRMS_SOURCE_CACHE_TTL_SECONDS = int(os.getenv("AGNIWATCH_FIRMS_CACHE_TTL", "600"))

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.getenv("AGNIWATCH_DATA_ROOT", str(PROJECT_ROOT / "Data")))
LOCAL_MAX_RECORDS = int(os.getenv("AGNIWATCH_LOCAL_MAX_RECORDS", "50000"))
DATA_MODE = os.getenv("AGNIWATCH_DATA_MODE", "LOCAL").upper()
MOCK_SEED = int(os.getenv("AGNIWATCH_MOCK_SEED", "42"))

# Demo (simulated) authentication credentials. NOT production-secure.
DEMO_USERNAME = os.getenv("AGNIWATCH_DEMO_USERNAME", "demo")
DEMO_PASSWORD = os.getenv("AGNIWATCH_DEMO_PASSWORD", "demo")

SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_TTL_SECONDS = int(os.getenv("AGNIWATCH_SESSION_TTL", "3600"))
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def get_cors_origins(value: Optional[str] = None) -> List[str]:
    configured = value if value is not None else os.getenv("AGNIWATCH_CORS_ORIGINS", "")
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if "*" in origins:
        raise ValueError("AGNIWATCH_CORS_ORIGINS must not contain '*' when credentials are enabled.")
    return list(dict.fromkeys((*DEFAULT_CORS_ORIGINS, *origins)))

# ---------------------------------------------------------------------------
# Shared observation schema
# ---------------------------------------------------------------------------


class Observation(BaseModel):
    observation_id: str
    record_id: str = ""
    source_sensor: str
    source_platform: Optional[str] = None
    product: str
    acq_datetime_utc: datetime
    acquisition_datetime_utc: Optional[datetime] = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    confidence: Optional[float | str] = None
    brightness: Optional[float] = None
    frp: Optional[float] = None
    area_of_interest: Optional[str] = None
    quality_flag: Optional[str] = None
    source: Optional[str] = None
    is_demo: bool = True

    @field_validator("acq_datetime_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("acq_datetime_utc must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("record_id", mode="before")
    @classmethod
    def _default_record_id(cls, value: Any, info: Any) -> str:
        if value:
            return str(value)
        return str(getattr(info.data, "observation_id", "") or "")

    @field_validator("acquisition_datetime_utc", mode="before")
    @classmethod
    def _sync_acquisition(cls, value: Any, info: Any) -> Any:
        if value is None and info.data.get("acq_datetime_utc") is not None:
            return info.data["acq_datetime_utc"]
        return value


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


def observation_to_dict(item: Observation) -> Dict[str, Any]:
    payload = item.model_dump(mode="json")
    payload.setdefault("record_id", payload.get("observation_id"))
    payload.setdefault("acq_datetime_utc", payload.get("acquisition_datetime_utc"))
    return payload


# ---------------------------------------------------------------------------
# NASA FIRMS provider (real mode)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# NASA FIRMS provider (real mode)
# ---------------------------------------------------------------------------


def _parse_firms_datetime(row: Dict[str, str]) -> datetime:
    raw_date = row.get("acq_date", "")
    raw_time = row.get("acq_time", "").zfill(4)
    return datetime.strptime(f"{raw_date} {raw_time}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)


def _clamp_days(days: int) -> int:
    return max(FIRMS_MIN_DAYS, min(int(days), FIRMS_MAX_DAYS))


def _fetch_firms_source(sensor: str, days: int) -> List[Observation]:
    """Fetch one sensor's active-fire CSV from the documented NASA FIRMS area API.

    Documented format: /api/area/csv/{MAP_KEY}/{SOURCE}/{west},{south},{east},{north}/{DAY_RANGE}
    """
    map_key = os.getenv("FIRMS_MAP_KEY")
    if not map_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Real data mode requires FIRMS_MAP_KEY on the backend")
    source = FIRMS_SOURCES[sensor]
    west, south, east, north = DEFAULT_BBOX
    days = _clamp_days(days)
    url = f"{FIRMS_API}/csv/{quote(map_key)}/{source}/{west},{south},{east},{north}/{days}"
    try:
        with urlopen(Request(url, headers={"User-Agent": "AgniWatch/0.3"}), timeout=20) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as error:
        if error.code in (401, 403):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"NASA FIRMS authentication failed for {sensor}: invalid or missing MAP_KEY",
            ) from error
        if error.code == 429:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"NASA FIRMS rate limit reached for {sensor}; retry after the 10-minute window",
            ) from error
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"NASA FIRMS request failed for {sensor}: HTTP {error.code}") from error
    except (URLError, TimeoutError) as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"NASA FIRMS network request failed for {sensor}: {error}") from error
    if payload.lstrip().startswith("<") or payload.startswith("Invalid"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"NASA FIRMS returned an invalid response for {sensor}")
    records: List[Observation] = []
    for row in csv.DictReader(io.StringIO(payload)):
        try:
            acquisition = _parse_firms_datetime(row)
            latitude = float(row["latitude"])
            longitude = float(row["longitude"])
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError("coordinates out of valid range")
            satellite = row.get("satellite") or None
            records.append(
                Observation(
                    observation_id=f"{sensor.lower()}-firms-{row.get('acq_date')}-{row.get('acq_time')}-{row.get('latitude')}-{row.get('longitude')}",
                    record_id=f"{sensor.lower()}-firms-{row.get('acq_date')}-{row.get('acq_time')}-{row.get('latitude')}-{row.get('longitude')}",
                    source_sensor=sensor,
                    source_platform=satellite,
                    product=source,
                    acq_datetime_utc=acquisition,
                    acquisition_datetime_utc=acquisition,
                    latitude=latitude,
                    longitude=longitude,
                    confidence=row.get("confidence") or None,
                    frp=float(row["frp"]) if row.get("frp") else None,
                    quality_flag=row.get("daynight") or None,
                    source="NASA FIRMS NRT",
                    is_demo=False,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"NASA FIRMS returned an invalid {sensor} record: {error}") from error
    return records


# Per-source cache so transient failures can serve stale NRT records.
_FIRMS_SOURCE_CACHE: Dict[str, Dict] = {}
_ACTIVE_CACHE: Optional[Dict] = None


def _get_firms_source(sensor: str, days: int) -> Dict:
    now = datetime.now(timezone.utc)
    entry = _FIRMS_SOURCE_CACHE.get(sensor)
    if entry and (now - entry["retrieved_at"]).total_seconds() < FIRMS_SOURCE_CACHE_TTL_SECONDS:
        return {"records": entry["records"], "freshness": "cached", "retrieved_at": entry["retrieved_at"], "warning": None}
    try:
        records = _fetch_firms_source(sensor, days)
    except HTTPException as error:
        if entry:
            return {
                "records": entry["records"],
                "freshness": "cached",
                "retrieved_at": entry["retrieved_at"],
                "warning": f"live fetch failed ({error.detail}); showing stale cached data",
            }
        raise
    retrieved_at = datetime.now(timezone.utc)
    _FIRMS_SOURCE_CACHE[sensor] = {"records": records, "retrieved_at": retrieved_at}
    return {"records": records, "freshness": "live", "retrieved_at": retrieved_at, "warning": None}

# ---------------------------------------------------------------------------
# Mock data provider (demo mode)
# ---------------------------------------------------------------------------


MOCK_LOCATIONS = [
    {"name": "Surat, Gujarat", "lat": 21.1702, "lon": 72.8311, "aoi": "surat"},
    {"name": "Mumbai, Maharashtra", "lat": 19.0760, "lon": 72.8777, "aoi": "mumbai"},
    {"name": "Delhi NCR", "lat": 28.6139, "lon": 77.2090, "aoi": "delhi"},
    {"name": "Chennai, Tamil Nadu", "lat": 13.0827, "lon": 80.2707, "aoi": "chennai"},
    {"name": "Kolkata, West Bengal", "lat": 22.5726, "lon": 88.3639, "aoi": "kolkata"},
    {"name": "Bengaluru, Karnataka", "lat": 12.9716, "lon": 77.5946, "aoi": "bengaluru"},
    {"name": "Hyderabad, Telangana", "lat": 17.3850, "lon": 78.4867, "aoi": "hyderabad"},
    {"name": "Ahmedabad, Gujarat", "lat": 23.0225, "lon": 72.5714, "aoi": "ahmedabad"},
    {"name": "Pune, Maharashtra", "lat": 18.5204, "lon": 73.8567, "aoi": "pune"},
]

SENSORS = ["MODIS", "VIIRS"]
PLATFORMS = {"MODIS": ["Terra", "Aqua"], "VIIRS": ["Suomi-NPP", "NOAA-20"]}
PRODUCTS = {"MODIS": ["MOD14A1", "MYD14A1"], "VIIRS": ["VNP14IMG", "VJ114IMG"]}


def jitter(value: float, spread: float = 0.03, rng: random.Random = None) -> float:
    rng = rng or random
    return round(value + rng.uniform(-spread, spread), 4)


def generate_mock_observations(
    start_date: date | None = None,
    end_date: date | None = None,
    seed: int = MOCK_SEED,
) -> List[Observation]:
    """Generate a deterministic, scientifically plausible synthetic dataset.

    Records are explicitly labelled is_demo=True and must never be presented as
    genuine NASA observations. Values are representative sample magnitudes only.
    """
    rng = random.Random(seed)
    start = start_date or (date(2025, 8, 1))
    end = end_date or (start + timedelta(days=89))
    records: List[Observation] = []
    counter = 0
    for loc in MOCK_LOCATIONS:
        # Each location forms a geographic cluster of observations.
        cluster_size = rng.randint(4, 10)
        for _ in range(cluster_size):
            counter += 1
            sensor = rng.choice(SENSORS)
            day_offset = rng.randint(0, (end - start).days)
            hour = rng.randint(0, 23)
            minute = rng.randint(0, 59)
            acquired = datetime(start.year, start.month, start.day, hour, minute, tzinfo=timezone.utc) + timedelta(days=day_offset)
            platform = rng.choice(PLATFORMS[sensor])
            product = rng.choice(PRODUCTS[sensor])
            confidence = rng.choice([30, 45, 58, 67, 74, 82, 91, 96])
            brightness = round(rng.uniform(300.0, 330.0), 1)
            frp = round(rng.uniform(2.0, 45.0), 1)
            quality = rng.choice(["nominal", "nominal", "nominal", "missing-frp", "low-confidence"])
            records.append(
                Observation(
                    observation_id=f"demo-{sensor.lower()}-{counter:04d}",
                    record_id=f"demo-{sensor.lower()}-{counter:04d}",
                    source_sensor=sensor,
                    source_platform=platform,
                    product=f"{product} demo fixture",
                    acq_datetime_utc=acquired,
                    acquisition_datetime_utc=acquired,
                    latitude=jitter(loc["lat"], rng=rng),
                    longitude=jitter(loc["lon"], rng=rng),
                    confidence=float(confidence),
                    brightness=brightness,
                    frp=frp if quality != "missing-frp" else None,
                    area_of_interest=loc["aoi"],
                    quality_flag=quality,
                    source="AgniWatch synthetic demo fixture",
                    is_demo=True,
                )
            )
    return records


MOCK_CACHE: Optional[List[Observation]] = None


def load_mock_records() -> List[Observation]:
    global MOCK_CACHE
    if MOCK_CACHE is None:
        MOCK_CACHE = generate_mock_observations()
    return MOCK_CACHE

# ---------------------------------------------------------------------------
# Data provider abstraction
# ---------------------------------------------------------------------------


class DataProvider(ABC):
    @abstractmethod
    def status(self) -> str:
        ...

    @abstractmethod
    def all_records(self) -> List[Observation]:
        ...

    @abstractmethod
    def metadata(self) -> Dict[str, Any]:
        ...


class MockDataProvider(DataProvider):
    def status(self) -> str:
        return "DEMO"

    def all_records(self) -> List[Observation]:
        return load_mock_records()

    def metadata(self) -> Dict[str, Any]:
        return {
            "status": "DEMO",
            "source": "AgniWatch synthetic demo fixture",
            "products": ["MODIS demo fixture", "VIIRS demo fixture"],
            "spatial_grid": "0.1 degree display grid",
            "time_bin": "UTC calendar day",
            "retrieved_at": None,
            "freshness": None,
            "warnings": [],
            "area": {"west": DEFAULT_BBOX[0], "south": DEFAULT_BBOX[1], "east": DEFAULT_BBOX[2], "north": DEFAULT_BBOX[3]},
            "is_demo": True,
            "note": "Synthetic demonstration data; not NASA observations.",
        }


class FirmsDataProvider(DataProvider):
    def __init__(self) -> None:
        self.days = FIRMS_MAX_DAYS

    def status(self) -> str:
        return "REAL"

    def all_records(self) -> List[Observation]:
        records: List[Observation] = []
        for sensor in FIRMS_SOURCES:
            got = _get_firms_source(sensor, self.days)
            records.extend(got["records"])
        return records

    def metadata(self) -> Dict[str, Any]:
        snapshot = self._snapshot()
        return {
            "status": "REAL",
            "source": "NASA FIRMS near-real-time area API",
            "products": ["MODIS_NRT", "VIIRS_SNPP_NRT"],
            "spatial_grid": "0.1 degree display grid",
            "time_bin": "UTC calendar day",
            "retrieved_at": snapshot["retrieved_at"],
            "freshness": snapshot["freshness"],
            "warnings": snapshot["warnings"],
            "area": {"west": DEFAULT_BBOX[0], "south": DEFAULT_BBOX[1], "east": DEFAULT_BBOX[2], "north": DEFAULT_BBOX[3]},
            "is_demo": False,
            "real_mode_note": "FIRMS area API provides recent NRT data (documented 1-5 day windows); archive downloads require separate Earthdata access.",
        }

    def _snapshot(self) -> Dict[str, Any]:
        freshnesses: List[str] = []
        warnings: List[str] = []
        retrieved_at: Optional[datetime] = None
        for sensor in FIRMS_SOURCES:
            got = _get_firms_source(sensor, self.days)
            if got["freshness"]:
                freshnesses.append(got["freshness"])
            if got.get("warning"):
                warnings.append(f"{sensor}: {got['warning']}")
            if got["retrieved_at"] and (retrieved_at is None or got["retrieved_at"] > retrieved_at):
                retrieved_at = got["retrieved_at"]
        freshness = "live" if "live" in freshnesses else ("cached" if freshnesses else None)
        return {"freshness": freshness, "retrieved_at": retrieved_at, "warnings": warnings}


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def build_provider() -> DataProvider:
    set_observation_model(Observation)
    if DATA_MODE == "LOCAL":
        return LocalDatasetProvider(DATA_ROOT, LOCAL_MAX_RECORDS)
    if DATA_MODE == "LOCAL_PARTIAL":
        return LocalDatasetProvider(DATA_ROOT, LOCAL_MAX_RECORDS, allow_partial=True)
    if DATA_MODE == "REAL":
        return FirmsDataProvider()
    return MockDataProvider()


PROVIDER = build_provider()


def filter_records(
    records: List[Observation],
    start: Optional[date] = None,
    end: Optional[date] = None,
    sensors: Optional[List[str]] = None,
    area: Optional[str] = None,
    west: Optional[float] = None,
    south: Optional[float] = None,
    east: Optional[float] = None,
    north: Optional[float] = None,
) -> List[Observation]:
    selected = {str(sensor).upper() for sensor in (sensors or [])}
    area_filter = (area or "").strip().lower()
    out: List[Observation] = []
    for item in records:
        if start and item.acq_datetime_utc.date() < start:
            continue
        if end and item.acq_datetime_utc.date() > end:
            continue
        if selected and item.source_sensor.upper() not in selected:
            continue
        if area_filter and area_filter != "all" and (item.area_of_interest or "").lower() != area_filter:
            continue
        if west is not None and item.longitude < west:
            continue
        if east is not None and item.longitude > east:
            continue
        if south is not None and item.latitude < south:
            continue
        if north is not None and item.latitude > north:
            continue
        out.append(item)
    return out


def _local_filters(start, end, sensors, area, west=None, south=None, east=None, north=None):
    return {
        "start": start, "end": end, "sensors": sensors, "area": area,
        "west": west, "south": south, "east": east, "north": north,
    }

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="AgniWatch API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)


def current_user(session_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not session_token:
        return None
    session = SESSIONS.get(session_token)
    if not session:
        return None
    expires_at = session.get("expires_at")
    if expires_at and datetime.now(timezone.utc) > expires_at:
        SESSIONS.pop(session_token, None)
        return None
    return session["user"]

@app.get("/api/health")
def health() -> Dict[str, Any]:
    provider_status = PROVIDER.status()
    return {
        "status": "ok",
        "data_status": provider_status,
        "message": (
            "NASA local datasets loaded"
            if provider_status == "LOCAL"
            else "NASA data — partially indexed"
            if provider_status == "PARTIAL"
            else "Synthetic demo fixture loaded"
            if provider_status == "DEMO"
            else "NASA FIRMS NRT mode configured"
            if provider_status == "REAL"
            else "NASA data sources are unavailable"
        ),
    }


@app.get("/api/metadata")
def metadata() -> Dict[str, Any]:
    snap = PROVIDER.metadata()
    return {
        "status": snap["status"],
        "source": snap["source"],
        "products": snap["products"],
        "spatial_grid": snap["spatial_grid"],
        "time_bin": snap["time_bin"],
        "retrieved_at": _iso(snap["retrieved_at"]),
        "area": snap["area"],
        "freshness": snap["freshness"],
        "warnings": snap["warnings"],
        "is_demo": snap.get("is_demo", True),
        "real_mode_note": snap.get("real_mode_note"),
        "datasets": snap.get("datasets", {}),
        "records_loaded": snap.get("records_loaded"),
        "date_range": snap.get("date_range"),
        "data_root": snap.get("data_root"),
        "record_cap": snap.get("record_cap"),
        "configured_record_cap": snap.get("configured_record_cap"),
        "index_ready": snap.get("index_ready"),
        "index_state": snap.get("index_state"),
        "note": snap.get("note"),
    }


@app.post("/api/auth/login")
def auth_login(credentials: Optional[LoginRequest] = None) -> Dict[str, Any]:
    """Demo/simulated login. Disclosed as not production-secure."""
    if credentials is None:
        username, password = "", ""
    else:
        username, password = credentials.username, credentials.password
    if username == DEMO_USERNAME and password == DEMO_PASSWORD:
        token = secrets.token_urlsafe(32)
        user = {"username": username, "role": "analyst", "name": "Demo Analyst", "auth_mode": "simulated"}
        SESSIONS[token] = {"user": user, "expires_at": datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)}
        return {"access_token": token, "token_type": "Bearer", "user": user, "demo": True, "auth_mode": "simulated"}
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid demo credentials. Use username: demo / password: demo")


@app.post("/api/auth/logout")
def auth_logout(session_token: Optional[str] = None) -> Dict[str, Any]:
    if session_token:
        SESSIONS.pop(session_token, None)
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(session_token: Optional[str] = Query(None, alias="session_token")) -> Dict[str, Any]:
    user = current_user(session_token)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return {"authenticated": True, "user": user, "auth_mode": "simulated"}

@app.get("/api/observations")
def observations(
    start: Optional[date] = None,
    end: Optional[date] = None,
    sensors: Annotated[Optional[List[str]], Query()] = None,
    area: Optional[str] = None,
    west: Optional[float] = Query(None, ge=-180, le=180),
    south: Optional[float] = Query(None, ge=-90, le=90),
    east: Optional[float] = Query(None, ge=-180, le=180),
    north: Optional[float] = Query(None, ge=-90, le=90),
    offset: int = Query(0, ge=0),
    limit: int = Query(5000, ge=1, le=10000),
) -> Dict[str, Any]:
    if start and end and start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start must be on or before end")
    if west is not None and east is not None and west > east:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "west must be on or before east")
    if south is not None and north is not None and south > north:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "south must be on or before north")
    snap = PROVIDER.metadata()
    if isinstance(PROVIDER, LocalDatasetProvider):
        records, total = PROVIDER.query_records(**_local_filters(start, end, sensors, area, west, south, east, north), offset=offset, limit=limit)
    else:
        filtered = filter_records(PROVIDER.all_records(), start, end, sensors, area, west, south, east, north)
        total = len(filtered)
        records = filtered[offset:offset + limit]
    return {
        "status": PROVIDER.status(),
        "freshness": snap.get("freshness"),
        "retrieved_at": _iso(snap.get("retrieved_at")),
        "is_demo": snap.get("is_demo", True),
        "total_records": total,
        "offset": offset,
        "limit": limit,
        "records": [observation_to_dict(item) for item in records],
    }


@app.get("/api/summary")
def summary(
    start: Optional[date] = None,
    end: Optional[date] = None,
    sensors: Annotated[Optional[List[str]], Query()] = None,
    area: Optional[str] = None,
) -> Dict[str, Any]:
    if isinstance(PROVIDER, LocalDatasetProvider):
        metrics = PROVIDER.summary(**_local_filters(start, end, sensors, area))
        return {
            "status": PROVIDER.status(), "total_records": metrics["total"],
            "by_sensor": {"MODIS": metrics["modis"], "VIIRS": metrics["viirs"]},
            "coverage": {"start": metrics["start"], "end": metrics["end"]},
            "confidence": {"available": metrics["confidence_numeric"] > 0, "numeric_count": metrics["confidence_numeric"]},
            "frp": {"available": metrics["frp_min"] is not None, "min": metrics["frp_min"], "max": metrics["frp_max"]},
        }
    records = filter_records(PROVIDER.all_records(), start, end, sensors, area)
    by_sensor = {sensor: sum(1 for item in records if item.source_sensor == sensor) for sensor in ("MODIS", "VIIRS")}
    dates = [item.acq_datetime_utc.date() for item in records]
    return {
        "status": PROVIDER.status(),
        "total_records": len(records),
        "by_sensor": by_sensor,
        "coverage": {"start": min(dates).isoformat() if dates else None, "end": max(dates).isoformat() if dates else None},
        "confidence": {
            "available": any(item.confidence is not None for item in records),
            "numeric_count": sum(1 for item in records if isinstance(item.confidence, (int, float))),
        },
        "frp": {
            "available": any(item.frp is not None for item in records),
            "min": min((item.frp for item in records if item.frp is not None), default=None),
            "max": max((item.frp for item in records if item.frp is not None), default=None),
        },
    }


@app.get("/api/activity-calendar")
def activity_calendar(
    start: Optional[date] = None,
    end: Optional[date] = None,
    sensors: Annotated[Optional[List[str]], Query()] = None,
    area: Optional[str] = None,
) -> Dict[str, Any]:
    if isinstance(PROVIDER, LocalDatasetProvider):
        days = PROVIDER.grouped_counts("day", **_local_filters(start, end, sensors, area))
        return {"status": PROVIDER.status(), "metric": "Number of returned observation records per UTC day", "days": days}
    records = filter_records(PROVIDER.all_records(), start, end, sensors, area)
    days: Dict[str, Dict[str, int]] = {}
    for item in records:
        key = item.acq_datetime_utc.date().isoformat()
        days.setdefault(key, {"MODIS": 0, "VIIRS": 0})
        days[key][item.source_sensor] += 1
    return {"status": PROVIDER.status(), "metric": "Number of returned observation records per UTC day", "days": days}


@app.get("/api/trends")
def trends(
    start: Optional[date] = None,
    end: Optional[date] = None,
    sensors: Annotated[Optional[List[str]], Query()] = None,
    area: Optional[str] = None,
) -> Dict[str, Any]:
    if isinstance(PROVIDER, LocalDatasetProvider):
        days = PROVIDER.grouped_counts("day", **_local_filters(start, end, sensors, area))
        if start and end:
            keys = [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]
        else:
            keys = sorted(days)
        return {
            "status": PROVIDER.status(), "metric": "Daily observation-record count",
            "points": [{"date": key, **days.get(key, {"MODIS": 0, "VIIRS": 0})} for key in keys],
        }
    records = filter_records(PROVIDER.all_records(), start, end, sensors, area)
    days: Dict[str, Dict[str, int]] = {}
    for item in records:
        key = item.acq_datetime_utc.date().isoformat()
        days.setdefault(key, {"MODIS": 0, "VIIRS": 0})
        days[key][item.source_sensor] += 1
    if start and end:
        keys = [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]
    else:
        keys = sorted(days)
    return {
        "status": PROVIDER.status(),
        "metric": "Daily observation-record count",
        "points": [{"date": key, **days.get(key, {"MODIS": 0, "VIIRS": 0})} for key in keys],
    }


@app.get("/api/methodology")
def methodology() -> Dict[str, str]:
    return {
        "standardization": "Source records are validated, timestamps are normalized to UTC, and sensor identity is retained.",
        "comparison": "MODIS and VIIRS counts are displayed separately; they are not equivalent measures of intensity, area, or severity.",
        "missing_values": "Missing source values remain null and are not converted to zero.",
        "aggregation": "The dashboard uses a 0.1-degree display grid and UTC calendar-day bins; it does not merge records or infer duplicate detections.",
        "limitations": "Local mode uses downloaded NASA CSV datasets and a resumable SQLite index; API responses are paginated rather than capped by AGNIWATCH_LOCAL_MAX_RECORDS. Real mode uses the FIRMS NRT area API (1-5 day windows). Counts are observation records, not confirmed fires.",
    }


@app.get("/api/export")
def export_csv(
    start: Optional[date] = None,
    end: Optional[date] = None,
    sensors: Annotated[Optional[List[str]], Query()] = None,
    area: Optional[str] = None,
) -> StreamingResponse:
    # Stable, documented field order for export.
    fields = [
        "observation_id", "record_id", "source_sensor", "source_platform", "product",
        "acq_datetime_utc", "acquisition_datetime_utc", "latitude", "longitude",
        "confidence", "brightness", "frp", "area_of_interest", "quality_flag",
        "source", "is_demo",
    ]

    def chunks():
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        yield output.getvalue()
        if isinstance(PROVIDER, LocalDatasetProvider):
            offset = 0
            while True:
                records, total = PROVIDER.query_records(
                    **_local_filters(start, end, sensors, area), offset=offset, limit=5000
                )
                if not records:
                    break
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
                for item in records:
                    writer.writerow(observation_to_dict(item))
                yield output.getvalue()
                offset += len(records)
                if offset >= total:
                    break
        else:
            records = filter_records(PROVIDER.all_records(), start, end, sensors, area)
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            for item in records:
                writer.writerow(observation_to_dict(item))
            yield output.getvalue()

    return StreamingResponse(
        chunks(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="agniwatch-observations.csv"'},
    )
