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
GTFS_STATIC_STALE_DAYS = int(os.environ.get("GTFS_STATIC_STALE_DAYS", "3"))
GTFS_ZIP_URL_TMPL = (
    "https://motionbuscard.org.cy/opendata/downloadfile?"
    "file=GTFS%5C{ag}_google_transit.zip&rel=True"
)

STOPS_MAX = 5314
VEHICLES_MAX = 731

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
    for ent in feed.entity:
        tu = ent.trip_update if ent.HasField("trip_update") else None
        if tu:
            t = tu.trip
            if t.trip_id: trips.add(t.trip_id)
            if t.route_id: routes.add(t.route_id)
            if tu.vehicle and tu.vehicle.id:
                vehicles.add(tu.vehicle.id)
            for stu in tu.stop_time_update:
                if stu.stop_id: stops.add(stu.stop_id)
        if ent.HasField("vehicle"):
            v = ent.vehicle
            if v.vehicle and v.vehicle.id:
                vehicles.add(v.vehicle.id)
            if v.trip and v.trip.trip_id:
                trips.add(v.trip.trip_id)
            if v.trip and v.trip.route_id:
                routes.add(v.trip.route_id)
    return {
        "ts_utc": now_utc().isoformat(),
        "trips": sorted(trips),
        "vehicles": sorted(vehicles),
        "stops": sorted(stops),
        "routes": sorted(routes),
    }


def update_rolling(snapshot):
    """Append snapshot, drop entries older than WINDOW_HOURS."""
    buf = []
    if ROLLING_PATH.exists():
        try:
            buf = json.loads(ROLLING_PATH.read_text())
        except Exception:
            buf = []
    buf.append(snapshot)
    cutoff = now_utc() - timedelta(hours=WINDOW_HOURS)
    buf = [s for s in buf if datetime.fromisoformat(s["ts_utc"]) >= cutoff]
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


def compute_observed_last_6h(buf):
    """Aggregate the rolling buffer: unique trips/vehicles/stops in last 6h,
    plus km derived from the Option A trip_distances cache."""
    if not buf:
        return _empty_panel("GTFS-RT · MOTION ITS")

    trips_seen, vehicles_seen, stops_seen, routes_seen = set(), set(), set(), set()
    for s in buf:
        trips_seen.update(s.get("trips", []))
        vehicles_seen.update(s.get("vehicles", []))
        stops_seen.update(s.get("stops", []))
        routes_seen.update(s.get("routes", []))

    # km via Option A unfiltered cache
    trip_distances, _meta = load_trip_distances()
    total_m = sum(trip_distances.get(tid, 0.0) for tid in trips_seen)
    total_km = total_m / 1000.0

    lookup = route_to_agency()
    operators = sorted({lookup[r] for r in routes_seen if r in lookup})

    first_ts = buf[0].get("ts_utc")
    last_ts = buf[-1].get("ts_utc")

    return {
        "trips": len(trips_seen),
        "km": round(total_km, 1),
        "stops": min(len(stops_seen), STOPS_MAX),
        "operators": len(operators),
        "operators_list": operators,
        "vehicles_in_motion": min(len(vehicles_seen), VEHICLES_MAX),
        "source": "GTFS-RT · MOTION ITS",
        "probe_count": len(buf),
        "first_probe_utc": first_ts,
        "last_probe_utc": last_ts,
    }


def _empty_panel(source):
    return {
        "trips": 0, "km": 0.0, "stops": 0, "operators": 0,
        "operators_list": [], "source": source,
    }


def write_motion_stats(planned, observed):
    payload = {
        "schema": "v3.0-dual-window",
        "generated_at_utc": now_utc().isoformat(),
        "cyprus_local_time": now_cyprus().isoformat(timespec="seconds"),
        "window_hours": WINDOW_HOURS,
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
    try:
        feed = fetch_feed()
        snap = snapshot_from_feed(feed)
        buf = update_rolling(snap)
    except Exception as e:
        print(f"[ERROR] RT fetch failed: {e}", file=sys.stderr)
        errors.append(("rt_fetch", str(e)))
        # Still try to read buffer from disk for the observed window
        if ROLLING_PATH.exists():
            try:
                buf = json.loads(ROLLING_PATH.read_text())
            except Exception:
                buf = []

    # 4. Compute both windows and write dual-output
    try:
        schedule = load_today_schedule()
        planned = compute_planned_next_6h(schedule)
        observed = compute_observed_last_6h(buf)
        write_motion_stats(planned, observed)
    except Exception as e:
        print(f"[ERROR] dual-window write failed: {e}", file=sys.stderr)
        errors.append(("dual_window", str(e)))

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
