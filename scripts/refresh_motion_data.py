#!/usr/bin/env python3
"""
Refresh MOTION ITS data for inframind.eu website.

Dual 6-hour window:
  • planned_next_6h  — trips scheduled to start in the next 6 hours (GTFS static)
  • observed_last_6h — trips actually observed in the last 6 hours (GTFS-RT)

Outputs:
  website/data/motion-stats.json         — v3.0 dual-window (consumed by the site)
  website/data/_trip_distances.json      — Option A: ALL trips → km (unfiltered cache)
  website/data/_today_schedule.json      — today's filtered trips with first-dep + stops + km
  website/data/_rolling_snapshots.json   — internal RT buffer for the last 6h
  website/data/planned.json              — kept for backwards-compat (legacy site code)

Hard ceilings (NEVER exceeded on display):
  STOPS_MAX    = 5314
  VEHICLES_MAX = 731

Environment:
  MOTION_FEED_URL   GTFS-RT endpoint (default points to MOTION production)
  ROLLING_HOURS     window hours (default 6 — used for both panels)
  PLANNED_REGEN     'always' to force schedule + trip-distances rebuild

Dependencies: requests, gtfs-realtime-bindings, protobuf
"""
import json
import math
import os
import sys
import zipfile
import csv
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

# ─── Paths ─────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
GTFS_STATIC_DIR = REPO_ROOT / "gtfs"
DATA_DIR.mkdir(parents=True, exist_ok=True)

TRIP_DIST_PATH = DATA_DIR / "_trip_distances.json"
TODAY_SCHED_PATH = DATA_DIR / "_today_schedule.json"
ROLLING_PATH = DATA_DIR / "_rolling_snapshots.json"
VEHICLE_QA_PATH = DATA_DIR / "_vehicle_qa.json"
MOTION_STATS_PATH = DATA_DIR / "motion-stats.json"
PLANNED_PATH = DATA_DIR / "planned.json"

# ─── Config ────────────────────────────────────────────────────────────────
FEED_URL = os.environ.get(
    "MOTION_FEED_URL",
    "http://20.19.98.194:8328/Api/api/gtfs-realtime",
)
WINDOW_HOURS = int(os.environ.get("ROLLING_HOURS", "6"))
WINDOW_SECONDS = WINDOW_HOURS * 3600
PLANNED_REGEN = os.environ.get("PLANNED_REGEN", "auto")
TRIP_DIST_STALE_DAYS = 7
GTFS_STATIC_STALE_DAYS = int(os.environ.get("GTFS_STATIC_STALE_DAYS", "1"))
# A live RT fetch failure (e.g. MOTION returns HTTP 500) is treated as a
# *transient* blip — non-fatal — only while the rolling buffer still holds a
# probe newer than this many minutes. Past that, the data is going stale and
# the run is failed on purpose so the failure surfaces (email + skipped commit)
# and the operator keeps control. Raise to tolerate longer MOTION outages.
RT_STALE_MINUTES = int(os.environ.get("RT_STALE_MINUTES", "30"))
GTFS_ZIP_URL_TMPL = (
    "https://motionbuscard.org.cy/opendata/downloadfile?"
    "file=GTFS%5C{ag}_google_transit.zip&rel=True"
)

STOPS_MAX = 5314
VEHICLES_MAX = 731
# Defensive ceiling for the hourly stop-arrivals counter (731 vehicles × ~1
# stop/min would be ~44k absolute theoretical max; normal peak is ~6-8k).
ARRIVALS_HOUR_MAX = 20000

AGENCY_NAMES = {
    "2": "ΟΣΥΠΑ", "4": "ΟΣΕΑ", "5": "INTERCITY",
    "6": "ΕΜΕΛ", "9": "NPT", "10": "LPT", "11": "PAME",
}
AGENCY_IDS = list(AGENCY_NAMES.keys())


# ─── Time helpers ──────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)


def now_cyprus():
    """Cyprus local time (EEST = UTC+3 in summer; approximation for date label)."""
    return now_utc() + timedelta(hours=3)


def cyprus_seconds_since_midnight():
    n = now_cyprus()
    return n.hour * 3600 + n.minute * 60 + n.second


def gtfs_date_str(d):
    return d.strftime("%Y%m%d")


def parse_gtfs_time_to_seconds(t):
    """GTFS times can exceed 24:00:00 (late-night services); return total seconds."""
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return None


# ─── Fresh GTFS static download ────────────────────────────────────────────
def gtfs_zip_age_days(zip_path):
    if not zip_path.exists():
        return None
    age_s = (datetime.now().timestamp() - zip_path.stat().st_mtime)
    return age_s / 86400.0


def refresh_gtfs_static_if_stale():
    """Download fresh GTFS zips from MOTION open-data portal when local copies
    are older than GTFS_STATIC_STALE_DAYS. Newest trip_ids in trips.txt must
    match the RT feed; stale zips → trip_id mismatch → observed km undercounts.
    Network errors are tolerated (we keep what we have).
    """
    refreshed = []
    for ag in AGENCY_IDS:
        zip_path = GTFS_STATIC_DIR / f"{ag}_google_transit.zip"
        age = gtfs_zip_age_days(zip_path)
        if age is not None and age < GTFS_STATIC_STALE_DAYS:
            continue
        url = GTFS_ZIP_URL_TMPL.format(ag=ag)
        tmp = zip_path.with_suffix(".zip.new")
        try:
            r = requests.get(url, timeout=60, stream=True)
            r.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
            # Validate it's actually a zip (sanity check before clobbering)
            with zipfile.ZipFile(tmp) as z:
                names = z.namelist()
                if "trips.txt" not in names:
                    raise ValueError("trips.txt missing from downloaded zip")
            tmp.replace(zip_path)
            refreshed.append(ag)
            print(f"[gtfs-static] refreshed agency {ag} ({zip_path.stat().st_size:,} bytes)")
        except Exception as e:
            print(f"[gtfs-static] skip agency {ag}: {e}", file=sys.stderr)
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    if refreshed:
        # Force downstream caches to rebuild since trip_ids may have changed
        try:
            TRIP_DIST_PATH.unlink()
        except FileNotFoundError:
            pass
        try:
            TODAY_SCHED_PATH.unlink()
        except FileNotFoundError:
            pass
        # Reset the cached route_to_agency mapping
        global _ROUTE_TO_AGENCY
        _ROUTE_TO_AGENCY = None
    return refreshed


# ─── Route → Agency lookup (cached singleton) ──────────────────────────────
_ROUTE_TO_AGENCY = None


def build_route_to_agency():
    """route_id → agency_name. Cyprus GTFS route_id prefix is NOT the agency_id."""
    mapping = {}
    for ag in AGENCY_IDS:
        zip_path = GTFS_STATIC_DIR / f"{ag}_google_transit.zip"
        if not zip_path.exists():
            continue
        try:
            with zipfile.ZipFile(zip_path) as z:
                with z.open("routes.txt") as f:
                    for row in csv.DictReader(
                        (line.decode("utf-8-sig") for line in f)
                    ):
                        rid = row.get("route_id", "").strip()
                        if rid:
                            mapping[rid] = AGENCY_NAMES[ag]
        except Exception as e:
            print(f"[route_lookup] skip {zip_path.name}: {e}", file=sys.stderr)
    return mapping


def route_to_agency():
    global _ROUTE_TO_AGENCY
    if _ROUTE_TO_AGENCY is None:
        _ROUTE_TO_AGENCY = build_route_to_agency()
        print(f"[route_lookup] {len(_ROUTE_TO_AGENCY)} route_id → agency mappings")
    return _ROUTE_TO_AGENCY


# ─── Distance helpers (haversine) ──────────────────────────────────────────
def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def compute_shape_lengths_from_zip(z):
    """shapes.txt → {shape_id: total_distance_m}."""
    points_by_shape = {}
    try:
        with z.open("shapes.txt") as f:
            for row in csv.DictReader(
                (line.decode("utf-8-sig") for line in f)
            ):
                sid = row.get("shape_id")
                if not sid:
                    continue
                try:
                    seq = int(row["shape_pt_sequence"])
                    lat = float(row["shape_pt_lat"])
                    lon = float(row["shape_pt_lon"])
                except (KeyError, ValueError):
                    continue
                points_by_shape.setdefault(sid, []).append((seq, lat, lon))
    except KeyError:
        return {}
    lengths = {}
    for sid, pts in points_by_shape.items():
        pts.sort(key=lambda x: x[0])
        total = 0.0
        for i in range(1, len(pts)):
            _, la1, lo1 = pts[i - 1]
            _, la2, lo2 = pts[i]
            total += haversine_m(la1, lo1, la2, lo2)
        lengths[sid] = total
    return lengths


# ─── Option A: unfiltered trip_distances cache ─────────────────────────────
# Maps EVERY trip_id (across all agencies, all service_ids) to its planned km.
# This is the canonical km lookup for ANY trip_id we observe in GTFS-RT,
# regardless of whether today's calendar_dates flags its service as active.
def build_trip_distances_cache():
    print("[trip_distances] rebuilding (Option A — unfiltered)…")
    trip_distances_m = {}
    trip_meta = {}
    for ag in AGENCY_IDS:
        zip_path = GTFS_STATIC_DIR / f"{ag}_google_transit.zip"
        if not zip_path.exists():
            print(f"[trip_distances] missing {zip_path.name}", file=sys.stderr)
            continue
        with zipfile.ZipFile(zip_path) as z:
            # trip → shape_id, route_id, service_id (NO service_id filter)
            trip_shape = {}
            with z.open("trips.txt") as f:
                for row in csv.DictReader(
                    (line.decode("utf-8-sig") for line in f)
                ):
                    tid = row.get("trip_id")
                    sid = row.get("shape_id")
                    rid = row.get("route_id")
                    svc = row.get("service_id")
                    if tid:
                        trip_shape[tid] = sid
                        trip_meta[tid] = {
                            "agency": AGENCY_NAMES[ag],
                            "route_id": rid,
                            "service_id": svc,
                        }
            shape_lengths = compute_shape_lengths_from_zip(z)
            for tid, sid in trip_shape.items():
                if sid and sid in shape_lengths:
                    trip_distances_m[tid] = shape_lengths[sid]
    payload = {
        "schema": "v2.0-trip-distances",
        "generated_at_utc": now_utc().isoformat(),
        "n_trips": len(trip_distances_m),
        "trip_distances_m": trip_distances_m,
        "trip_meta": trip_meta,
    }
    TRIP_DIST_PATH.write_text(json.dumps(payload, ensure_ascii=False))
    print(f"[trip_distances] cached {len(trip_distances_m)} trips → {TRIP_DIST_PATH.name}")
    return payload


def trip_distances_needs_refresh():
    if PLANNED_REGEN == "always":
        return True
    if not TRIP_DIST_PATH.exists():
        return True
    try:
        data = json.loads(TRIP_DIST_PATH.read_text())
        if not data.get("trip_distances_m"):
            return True
        gen = data.get("generated_at_utc")
        if not gen:
            return True
        age_days = (now_utc() - datetime.fromisoformat(gen)).total_seconds() / 86400
        if age_days > TRIP_DIST_STALE_DAYS:
            return True
    except Exception:
        return True
    return False


def load_trip_distances():
    if not TRIP_DIST_PATH.exists():
        return {}, {}
    try:
        data = json.loads(TRIP_DIST_PATH.read_text())
        return (
            data.get("trip_distances_m", {}) or {},
            data.get("trip_meta", {}) or {},
        )
    except Exception:
        return {}, {}


# ─── Today's schedule (filtered by calendar_dates) ──────────────────────────
def build_today_schedule():
    """Today's active trips with first departure time, stops, km.
    Filtered by calendar_dates active service_ids — this is what's "in the schedule
    for today". The unfiltered trip_distances cache is separate (covers all trips).
    """
    today_local = now_cyprus().date()
    target = gtfs_date_str(today_local)
    print(f"[today_schedule] building for {today_local}…")

    trip_distances, trip_meta = load_trip_distances()

    trips_today = {}  # trip_id → {first_dep_sec, agency, stops:[], km, route_id}
    for ag in AGENCY_IDS:
        zip_path = GTFS_STATIC_DIR / f"{ag}_google_transit.zip"
        if not zip_path.exists():
            continue
        with zipfile.ZipFile(zip_path) as z:
            # Active service_ids today
            services = set()
            with z.open("calendar_dates.txt") as f:
                for row in csv.DictReader(
                    (line.decode("utf-8-sig") for line in f)
                ):
                    if row["date"] == target and row["exception_type"] == "1":
                        services.add(row["service_id"])
            if not services:
                continue

            # Today's trip_ids (filtered)
            agency_today = set()
            trip_route = {}
            with z.open("trips.txt") as f:
                for row in csv.DictReader(
                    (line.decode("utf-8-sig") for line in f)
                ):
                    if row["service_id"] in services:
                        tid = row["trip_id"]
                        agency_today.add(tid)
                        trip_route[tid] = row.get("route_id")

            # stop_times → first departure + stops list per trip
            trip_first_seq = {}     # tid → (seq, dep_seconds)
            trip_stops = {}         # tid → set of stop_ids
            with z.open("stop_times.txt") as f:
                for row in csv.DictReader(
                    (line.decode("utf-8-sig") for line in f)
                ):
                    tid = row["trip_id"]
                    if tid not in agency_today:
                        continue
                    try:
                        seq = int(row.get("stop_sequence", "999"))
                    except ValueError:
                        seq = 999
                    stop_id = row.get("stop_id")
                    if stop_id:
                        trip_stops.setdefault(tid, set()).add(stop_id)
                    dep = (row.get("departure_time") or row.get("arrival_time") or "").strip()
                    dep_sec = parse_gtfs_time_to_seconds(dep)
                    if dep_sec is None:
                        continue
                    cur = trip_first_seq.get(tid)
                    if cur is None or seq < cur[0]:
                        trip_first_seq[tid] = (seq, dep_sec)

            for tid in agency_today:
                first = trip_first_seq.get(tid)
                if first is None:
                    continue
                _, dep_sec = first
                trips_today[tid] = {
                    "first_dep_sec": dep_sec,
                    "agency": AGENCY_NAMES[ag],
                    "stops": sorted(trip_stops.get(tid, [])),
                    "km": round(trip_distances.get(tid, 0.0) / 1000.0, 3),
                    "route_id": trip_route.get(tid),
                }

    payload = {
        "schema": "v1.0-today-schedule",
        "date": today_local.isoformat(),
        "generated_at_utc": now_utc().isoformat(),
        "window_hours": WINDOW_HOURS,
        "trips": trips_today,
    }
    TODAY_SCHED_PATH.write_text(json.dumps(payload, ensure_ascii=False))
    n_trips = len(trips_today)
    total_km = sum(t["km"] for t in trips_today.values())
    print(f"[today_schedule] {n_trips} trips · {total_km:.0f} km total · → {TODAY_SCHED_PATH.name}")

    # Also keep planned.json for backwards-compat with legacy index.html
    _write_legacy_planned(today_local, trips_today)
    return payload


def _write_legacy_planned(today_local, trips_today):
    """Legacy planned.json — kept for any UI that still reads it. Whole-day totals only."""
    operators = sorted({t["agency"] for t in trips_today.values()})
    stops_all = set()
    for t in trips_today.values():
        stops_all.update(t["stops"])
    hour_dist = {}
    for t in trips_today.values():
        h = (t["first_dep_sec"] // 3600) % 24
        hour_dist[h] = hour_dist.get(h, 0) + 1
    peak_hour = max(hour_dist, key=hour_dist.get) if hour_dist else None
    payload = {
        "schema": "v1.0-planned",
        "date": today_local.isoformat(),
        "generated_at_utc": now_utc().isoformat(),
        "trips_planned": len(trips_today),
        "stops_planned": min(len(stops_all), STOPS_MAX),
        "vehicles_ceiling": VEHICLES_MAX,
        "operators_active": len(operators),
        "operators_list": operators,
        "peak_hour": peak_hour,
        "peak_count": hour_dist.get(peak_hour, 0) if peak_hour is not None else 0,
        "hour_distribution": {str(k): v for k, v in hour_dist.items()},
    }
    PLANNED_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def today_schedule_needs_refresh():
    if PLANNED_REGEN == "always":
        return True
    if not TODAY_SCHED_PATH.exists():
        return True
    try:
        data = json.loads(TODAY_SCHED_PATH.read_text())
        return data.get("date") != now_cyprus().date().isoformat()
    except Exception:
        return True


def load_today_schedule():
    if not TODAY_SCHED_PATH.exists():
        return None
    try:
        return json.loads(TODAY_SCHED_PATH.read_text())
    except Exception:
        return None


# ─── GTFS-RT feed ──────────────────────────────────────────────────────────
def fetch_feed():
    print(f"[fetch] {FEED_URL}")
    r = requests.get(FEED_URL, timeout=30)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed


def snapshot_from_feed(feed):
    trips, vehicles, stops, routes = set(), set(), set(), set()
    # vehicle.id -> license_plate (verified 2026-06-10: coverage 332/332,
    # label == license_plate, id<->plate strictly 1:1). Stored per probe so
    # the QA pass can detect if that 1:1 invariant ever breaks upstream.
    vehicle_plates = {}
    # trip_id -> route_id for THIS probe. Persisted so observed km can be
    # estimated by route when a live trip_id is absent from the (slower-moving)
    # trip_distances cache. route_ids stay stable while trip_ids churn daily.
    trip_routes = {}
    # trip_id -> highest stop_sequence whose arrival time has already passed.
    # The per-probe progression of this value counts actual stop arrivals
    # (validated in motion-arrivals-lab, 2026-06-10: TripUpdate arrival times
    # are populated 582/582; VehiclePosition current_status is not).
    trip_progress = {}
    now_epoch = int(now_utc().timestamp())
    for ent in feed.entity:
        tu = ent.trip_update if ent.HasField("trip_update") else None
        if tu:
            t = tu.trip
            if t.trip_id: trips.add(t.trip_id)
            if t.route_id: routes.add(t.route_id)
            if t.trip_id and t.route_id: trip_routes[t.trip_id] = t.route_id
            if tu.vehicle and tu.vehicle.id:
                vehicles.add(tu.vehicle.id)
            passed_seq = 0
            for stu in tu.stop_time_update:
                if stu.stop_id: stops.add(stu.stop_id)
                if (stu.HasField("arrival") and stu.arrival.time
                        and stu.arrival.time <= now_epoch
                        and stu.stop_sequence > passed_seq):
                    passed_seq = stu.stop_sequence
            if t.trip_id and passed_seq:
                trip_progress[t.trip_id] = passed_seq
        if ent.HasField("vehicle"):
            v = ent.vehicle
            if v.vehicle and v.vehicle.id:
                vehicles.add(v.vehicle.id)
                if v.vehicle.license_plate:
                    vehicle_plates[v.vehicle.id] = v.vehicle.license_plate
            if v.trip and v.trip.trip_id:
                trips.add(v.trip.trip_id)
            if v.trip and v.trip.route_id:
                routes.add(v.trip.route_id)
            if v.trip and v.trip.trip_id and v.trip.route_id:
                trip_routes.setdefault(v.trip.trip_id, v.trip.route_id)
    return {
        "ts_utc": now_utc().isoformat(),
        "trips": sorted(trips),
        "vehicles": sorted(vehicles),
        "stops": sorted(stops),
        "routes": sorted(routes),
        "trip_routes": trip_routes,
        "vehicle_plates": vehicle_plates,
        "trip_progress": trip_progress,
    }


def _arrivals_delta(prev_progress, cur_progress):
    """Stop arrivals between two probes: sum of stop_sequence advances for
    trips present in BOTH probes. New trips are not counted on first sight
    (their past stops predate our observation) — same rule as the lab."""
    if not isinstance(prev_progress, dict) or not prev_progress:
        return 0
    total = 0
    for tid, cur_seq in cur_progress.items():
        prev_seq = prev_progress.get(tid)
        if prev_seq is not None and cur_seq > prev_seq:
            total += cur_seq - prev_seq
    return total


def update_rolling(snapshot):
    """Append snapshot, drop entries older than WINDOW_HOURS."""
    buf = []
    if ROLLING_PATH.exists():
        try:
            buf = json.loads(ROLLING_PATH.read_text())
        except Exception:
            buf = []
    # Count stop arrivals since the previous probe (TripUpdate progression)
    prev = buf[-1] if buf else None
    snapshot["arrivals_delta"] = _arrivals_delta(
        prev.get("trip_progress") if prev else None,
        snapshot.get("trip_progress") or {},
    )
    buf.append(snapshot)
    cutoff = now_utc() - timedelta(hours=WINDOW_HOURS)
    buf = [s for s in buf if datetime.fromisoformat(s["ts_utc"]) >= cutoff]
    # trip_progress is only needed to diff the NEXT probe — strip it from all
    # but the newest entry so the rolling file stays lean.
    for s in buf[:-1]:
        s.pop("trip_progress", None)
    ROLLING_PATH.write_text(json.dumps(buf, ensure_ascii=False))
    return buf


# ─── Window computations ───────────────────────────────────────────────────
def compute_planned_next_6h(schedule):
    """Trips whose first scheduled departure is in [now_cyprus, now_cyprus + 6h].
    Handles GTFS-overflow times (>24:00) — they belong to today's calendar day.
    Vehicles estimate: peak concurrent in 30-min buckets (typical trip ~45-60 min),
    capped at the licensed fleet ceiling.
    """
    if not schedule:
        return _empty_panel("GTFS static · MOTION ITS")

    now_sec = cyprus_seconds_since_midnight()
    end_sec = now_sec + WINDOW_SECONDS

    trips = schedule.get("trips", {})
    in_window = []
    for tid, t in trips.items():
        dep = t.get("first_dep_sec")
        if dep is None:
            continue
        if now_sec <= dep < end_sec:
            in_window.append((tid, t))

    n_trips = len(in_window)
    total_km = sum(t["km"] for _, t in in_window)
    agencies = sorted({t["agency"] for _, t in in_window})
    routes = {t.get("route_id") for _, t in in_window if t.get("route_id")}
    stops = set()
    for _, t in in_window:
        stops.update(t.get("stops", []))

    # ── Peak concurrent bus estimate ──
    # Bucket 30-min slots. Assume each trip occupies ~3 buckets (~90 min) as a
    # safe upper-bound for Cyprus PT (city routes shorter, intercity longer).
    # Concurrent buses in slot N ≈ trips_starting_in_slot_N + trips_started_in_slots_(N-1,N-2)
    BUCKET_SEC = 1800
    TRIP_BUCKETS = 3  # how many 30-min buckets a typical trip spans
    buckets = {}
    for _, t in in_window:
        dep = t["first_dep_sec"]
        b = (dep - now_sec) // BUCKET_SEC
        buckets[b] = buckets.get(b, 0) + 1
    # Sliding sum of TRIP_BUCKETS consecutive buckets gives concurrent vehicles
    bucket_keys = sorted(buckets.keys())
    if bucket_keys:
        max_b = max(bucket_keys)
        peak_concurrent = 0
        for i in range(0, max_b + 1):
            s = sum(buckets.get(j, 0) for j in range(max(0, i - TRIP_BUCKETS + 1), i + 1))
            if s > peak_concurrent:
                peak_concurrent = s
        vehicles_estimate = min(peak_concurrent, VEHICLES_MAX)
    else:
        vehicles_estimate = 0

    return {
        "trips": n_trips,
        "km": round(total_km, 1),
        "stops": min(len(stops), STOPS_MAX),
        "operators": len(agencies),
        "operators_list": agencies,
        "routes": len(routes),
        "vehicles_estimate": vehicles_estimate,
        "fleet_capacity": VEHICLES_MAX,
        "source": "GTFS static · MOTION ITS",
        "window_start_cyprus_seconds": now_sec,
        "window_end_cyprus_seconds": end_sec,
    }


def build_route_distance_table(trip_distances, meta):
    """route_id -> median trip distance in METRES, derived from the cache.
    route_ids are stable across days, so this table stays valid even when the
    live RT trip_ids have advanced beyond the cached trip_distances keys."""
    by_route = {}
    for tid, m in meta.items():
        r = m.get("route_id")
        d = trip_distances.get(tid)
        if r and d:
            by_route.setdefault(r, []).append(d)
    return {r: statistics.median(v) for r, v in by_route.items()}


def compute_observed_last_6h(buf):
    """Aggregate the rolling buffer: unique trips/vehicles/stops in last 6h,
    plus km. km is exact when the live trip_id is in the trip_distances cache;
    otherwise it's estimated from the trip's route median (route-based fallback)
    so the total survives the daily trip_id churn instead of collapsing to ~0."""
    if not buf:
        return _empty_panel("GTFS-RT · MOTION ITS")

    trips_seen, vehicles_seen, stops_seen, routes_seen = set(), set(), set(), set()
    live_trip_route = {}  # trip_id -> route_id, merged across probes
    for s in buf:
        trips_seen.update(s.get("trips", []))
        vehicles_seen.update(s.get("vehicles", []))
        stops_seen.update(s.get("stops", []))
        routes_seen.update(s.get("routes", []))
        tr = s.get("trip_routes")
        if isinstance(tr, dict):
            live_trip_route.update(tr)

    trip_distances, meta = load_trip_distances()
    route_dist_m = build_route_distance_table(trip_distances, meta)

    total_m = 0.0
    n_exact = n_estimated = n_unmatched = 0
    for tid in trips_seen:
        d = trip_distances.get(tid)
        if d:
            total_m += d
            n_exact += 1
            continue
        # Cache miss → estimate from the trip's route median.
        route = live_trip_route.get(tid) or (meta.get(tid, {}) or {}).get("route_id")
        rm = route_dist_m.get(route) if route else None
        if rm:
            total_m += rm
            n_estimated += 1
        else:
            n_unmatched += 1
    total_km = total_m / 1000.0

    lookup = route_to_agency()
    operators = sorted({lookup[r] for r in routes_seen if r in lookup})

    # Stop arrivals in the last hour: sum of per-probe progression deltas.
    cutoff_1h = now_utc() - timedelta(hours=1)
    arrivals_hour = sum(
        int(s.get("arrivals_delta") or 0)
        for s in buf
        if datetime.fromisoformat(s["ts_utc"]) >= cutoff_1h
    )

    first_ts = buf[0].get("ts_utc")
    last_ts = buf[-1].get("ts_utc")

    return {
        "trips": len(trips_seen),
        "km": round(total_km, 1),
        "stops": min(len(stops_seen), STOPS_MAX),
        "operators": len(operators),
        "operators_list": operators,
        "vehicles_in_motion": min(len(vehicles_seen), VEHICLES_MAX),
        "stop_arrivals_last_hour": min(arrivals_hour, ARRIVALS_HOUR_MAX),
        "source": "GTFS-RT · MOTION ITS",
        "probe_count": len(buf),
        "first_probe_utc": first_ts,
        "last_probe_utc": last_ts,
        # km provenance (transparency / monitoring; safe for the site to ignore)
        "km_exact_trips": n_exact,
        "km_estimated_trips": n_estimated,
        "km_unmatched_trips": n_unmatched,
    }


def write_vehicle_qa(buf):
    """Guard the vehicle-count integrity invariants across the rolling window.

    Verified baseline (2026-06-10): every VehiclePosition carries id + plate,
    and id<->plate is strictly 1:1. Our unique-vehicle counts dedup on
    vehicle.id, so if the upstream feed ever breaks these invariants the
    counts would silently drift. This writes aggregate totals only to
    data/_vehicle_qa.json and prints loud warnings on violation.
    """
    if not buf:
        return

    merged = {}          # id -> set(plates) across the window
    plate_ids = {}       # plate -> set(ids) across the window
    ids_seen, ids_with_plate = set(), set()
    for s in buf:
        vp = s.get("vehicle_plates")
        ids_seen.update(s.get("vehicles", []))
        if not isinstance(vp, dict):
            continue
        for vid, plate in vp.items():
            ids_with_plate.add(vid)
            merged.setdefault(vid, set()).add(plate)
            plate_ids.setdefault(plate, set()).add(vid)

    ids_multi_plate = sum(1 for v in merged.values() if len(v) > 1)
    plates_multi_id = sum(1 for v in plate_ids.values() if len(v) > 1)
    coverage_pct = (
        round(100.0 * len(ids_with_plate) / len(ids_seen), 1) if ids_seen else 0.0
    )

    payload = {
        "generated_at_utc": now_utc().isoformat(),
        "window_hours": WINDOW_HOURS,
        "probe_count": len(buf),
        "unique_vehicle_ids": len(ids_seen),
        "ids_with_plate": len(ids_with_plate),
        "plate_coverage_pct": coverage_pct,
        "unique_plates": len(plate_ids),
        "ids_mapping_to_multiple_plates": ids_multi_plate,
        "plates_mapping_to_multiple_ids": plates_multi_id,
        "invariant_1to1_ok": ids_multi_plate == 0 and plates_multi_id == 0,
    }
    VEHICLE_QA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    if ids_multi_plate or plates_multi_id:
        print(
            f"[WARN][vehicle-qa] 1:1 invariant BROKEN — "
            f"{ids_multi_plate} ids with >1 plate, "
            f"{plates_multi_id} plates with >1 id. "
            f"vehicles_in_motion may be over/under-counted.",
            file=sys.stderr,
        )
    else:
        print(
            f"[vehicle-qa] OK — {len(ids_seen)} ids, "
            f"plate coverage {coverage_pct}%, 1:1 invariant holds"
        )


def _empty_panel(source):
    return {
        "trips": 0, "km": 0.0, "stops": 0, "operators": 0,
        "operators_list": [], "source": source,
    }


def write_motion_stats(planned, observed, rt_degraded=False):
    payload = {
        "schema": "v3.0-dual-window",
        "generated_at_utc": now_utc().isoformat(),
        "cyprus_local_time": now_cyprus().isoformat(timespec="seconds"),
        "window_hours": WINDOW_HOURS,
        # True when this refresh served buffered data because the live RT feed
        # was unreachable but the buffer was still fresh (transient MOTION blip).
        "rt_degraded": rt_degraded,
        "planned_next_6h": planned,
        "observed_last_6h": observed,
    }
    MOTION_STATS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(
        f"[motion-stats] PLANNED next {WINDOW_HOURS}h: "
        f"{planned['trips']} trips · {planned['km']:.0f} km · "
        f"{planned['stops']} stops · {planned['operators']} ops"
    )
    print(
        f"[motion-stats] OBSERVED last {WINDOW_HOURS}h: "
        f"{observed['trips']} trips · {observed['km']:.0f} km · "
        f"{observed['stops']} stops · {observed['operators']} ops · "
        f"{observed.get('vehicles_in_motion', 0)} vehicles"
    )


# ─── Main ──────────────────────────────────────────────────────────────────
def main():
    errors = []

    # 0. Refresh GTFS static zips when stale (trip_id mismatch defence)
    try:
        refresh_gtfs_static_if_stale()
    except Exception as e:
        print(f"[ERROR] gtfs-static refresh failed: {e}", file=sys.stderr)
        errors.append(("gtfs_static", str(e)))

    # 1. Ensure trip_distances cache (Option A — unfiltered, all trips)
    try:
        if trip_distances_needs_refresh():
            build_trip_distances_cache()
        else:
            print("[trip_distances] cache fresh, skipping rebuild")
    except Exception as e:
        print(f"[ERROR] trip_distances build failed: {e}", file=sys.stderr)
        errors.append(("trip_distances", str(e)))

    # 2. Ensure today's schedule (filtered to today's calendar_dates)
    try:
        if today_schedule_needs_refresh():
            build_today_schedule()
        else:
            print("[today_schedule] fresh, skipping rebuild")
    except Exception as e:
        print(f"[ERROR] today_schedule build failed: {e}", file=sys.stderr)
        errors.append(("today_schedule", str(e)))

    # 3. Fetch live feed + update rolling buffer
    buf = []
    rt_degraded = False  # True when serving from a slightly-stale buffer
    try:
        feed = fetch_feed()
        snap = snapshot_from_feed(feed)
        buf = update_rolling(snap)
    except Exception as e:
        # Live RT fetch failed (e.g. MOTION HTTP 500). This is non-fatal ONLY
        # if the rolling buffer still holds a recent probe — otherwise the data
        # is going stale and we fail on purpose so the operator stays in control.
        if ROLLING_PATH.exists():
            try:
                buf = json.loads(ROLLING_PATH.read_text())
            except Exception:
                buf = []

        buffer_age_min = None
        if buf:
            try:
                last_probe = datetime.fromisoformat(buf[-1]["ts_utc"])
                buffer_age_min = (now_utc() - last_probe).total_seconds() / 60.0
            except Exception:
                buffer_age_min = None

        if buffer_age_min is not None and buffer_age_min <= RT_STALE_MINUTES:
            # Transient blip — we still have fresh data. Warn, don't fail.
            rt_degraded = True
            print(
                f"[WARN] RT fetch failed ({e}) — serving buffered data, "
                f"last probe {buffer_age_min:.1f} min old "
                f"(<= {RT_STALE_MINUTES} min threshold). Commit will proceed.",
                file=sys.stderr,
            )
        else:
            # No buffer, or buffer too old: real staleness — fail the run.
            age_txt = (
                f"{buffer_age_min:.1f} min old" if buffer_age_min is not None
                else "no buffer on disk"
            )
            print(
                f"[ERROR] RT fetch failed ({e}) and buffer is stale "
                f"({age_txt} > {RT_STALE_MINUTES} min threshold). Failing run.",
                file=sys.stderr,
            )
            errors.append(("rt_fetch", str(e)))

    # 4. Compute both windows and write dual-output
    try:
        schedule = load_today_schedule()
        planned = compute_planned_next_6h(schedule)
        observed = compute_observed_last_6h(buf)
        write_motion_stats(planned, observed, rt_degraded=rt_degraded)
    except Exception as e:
        print(f"[ERROR] dual-window write failed: {e}", file=sys.stderr)
        errors.append(("dual_window", str(e)))

    # 5. Vehicle-count QA (non-fatal — monitoring only)
    try:
        write_vehicle_qa(buf)
    except Exception as e:
        print(f"[WARN] vehicle-qa write failed: {e}", file=sys.stderr)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
