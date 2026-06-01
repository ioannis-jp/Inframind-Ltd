#!/usr/bin/env python3
"""
Refresh MOTION ITS data for inframind.eu website.

Triggered by Cloudflare Worker every 5 minutes (24/7) via GitHub workflow_dispatch.
Produces:

  website/data/motion-stats.json          — Hero B (executed last ~6h rolling window)
  website/data/planned.json               — Hero A (planned today from GTFS static, incl. km)
  website/data/_rolling_snapshots.json    — internal rolling buffer (committed for state)
  website/data/_trip_distances.json       — per-trip shape distance cache (rebuilt daily)
  website/data/daily_executed.json        — accumulator of trip_ids seen today
  website/data/daily_history.json         — finalized per-day totals, kept last 60 days
  website/data/verification_planned.json  — audit trail for km calculation

Hard ceilings enforced (NEVER exceeded on display):
  STOPS_MAX = 5314
  VEHICLES_MAX = 731

Environment variables:
  MOTION_FEED_URL   GTFS-RT endpoint (default: http://20.19.98.194:8328/Api/api/gtfs-realtime)
  ROLLING_HOURS     window for Hero B (default: 6)
  PLANNED_REGEN     'always' to force regenerate planned.json; default: regen if date changed

Dependencies:
  requests, gtfs-realtime-bindings, protobuf
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

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
GTFS_STATIC_DIR = REPO_ROOT / "gtfs"
DATA_DIR.mkdir(parents=True, exist_ok=True)

FEED_URL = os.environ.get(
    "MOTION_FEED_URL",
    "http://20.19.98.194:8328/Api/api/gtfs-realtime",
)
ROLLING_HOURS = int(os.environ.get("ROLLING_HOURS", "6"))
PLANNED_REGEN = os.environ.get("PLANNED_REGEN", "auto")

STOPS_MAX = 5314
VEHICLES_MAX = 731

AGENCY_NAMES = {
    "2": "ΟΣΥΠΑ", "4": "ΟΣΕΑ", "5": "INTERCITY",
    "6": "ΕΜΕΛ", "9": "NPT", "10": "LPT", "11": "PAME",
}
AGENCY_IDS = list(AGENCY_NAMES.keys())

DAILY_EXEC_PATH = DATA_DIR / "daily_executed.json"
DAILY_HISTORY_PATH = DATA_DIR / "daily_history.json"
TRIP_DIST_PATH = DATA_DIR / "_trip_distances.json"
HISTORY_KEEP_DAYS = 60


def build_route_to_agency():
    """Build a lookup route_id → agency_name from GTFS static routes.txt."""
    import zipfile as _zip
    mapping = {}
    for ag in AGENCY_IDS:
        zip_path = GTFS_STATIC_DIR / f"{ag}_google_transit.zip"
        if not zip_path.exists():
            continue
        try:
            with _zip.ZipFile(zip_path) as z:
                with z.open("routes.txt") as f:
                    reader = csv.DictReader(
                        (line.decode("utf-8-sig") for line in f)
                    )
                    name = AGENCY_NAMES[ag]
                    for row in reader:
                        rid = row.get("route_id", "").strip()
                        if rid:
                            mapping[rid] = name
        except Exception as e:
            print(f"[route_lookup] skip {zip_path.name}: {e}", file=sys.stderr)
    return mapping


_ROUTE_TO_AGENCY = None


def route_to_agency():
    global _ROUTE_TO_AGENCY
    if _ROUTE_TO_AGENCY is None:
        _ROUTE_TO_AGENCY = build_route_to_agency()
        print(f"[route_lookup] loaded {len(_ROUTE_TO_AGENCY)} route_id → agency mappings")
    return _ROUTE_TO_AGENCY


def now_utc():
    return datetime.now(timezone.utc)


def now_cyprus():
    # Cyprus is UTC+2 (EET) or UTC+3 (EEST). Approximation OK for date label.
    return now_utc() + timedelta(hours=3)


def fetch_feed():
    print(f"[fetch] {FEED_URL}")
    r = requests.get(FEED_URL, timeout=30)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed


def snapshot_from_feed(feed):
    """Extract unique IDs from a single feed snapshot."""
    trips, vehicles, stops, routes, operators = set(), set(), set(), set(), set()
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
    # Resolve operators via authoritative route_id → agency_name lookup
    lookup = route_to_agency()
    for rid in routes:
        name = lookup.get(rid)
        if name:
            operators.add(name)
    return {
        "ts_utc": now_utc().isoformat(),
        "trips": sorted(trips),
        "vehicles": sorted(vehicles),
        "stops": sorted(stops),
        "routes": sorted(routes),
        "operators": sorted(operators),
    }


def update_rolling(snapshot):
    """Append snapshot to rolling buffer, drop entries older than ROLLING_HOURS."""
    buf_path = DATA_DIR / "_rolling_snapshots.json"
    buf = []
    if buf_path.exists():
        try:
            buf = json.loads(buf_path.read_text())
        except Exception:
            buf = []
    buf.append(snapshot)
    cutoff = now_utc() - timedelta(hours=ROLLING_HOURS)
    buf = [s for s in buf if datetime.fromisoformat(s["ts_utc"]) >= cutoff]
    buf_path.write_text(json.dumps(buf, ensure_ascii=False))
    return buf


def aggregate_window(buf):
    trips, vehicles, stops, routes = set(), set(), set(), set()
    for s in buf:
        trips.update(s.get("trips", []))
        vehicles.update(s.get("vehicles", []))
        stops.update(s.get("stops", []))
        routes.update(s.get("routes", []))
    lookup = route_to_agency()
    operators = set()
    for rid in routes:
        name = lookup.get(rid)
        if name:
            operators.add(name)
    first = buf[0]["ts_utc"] if buf else None
    last = buf[-1]["ts_utc"] if buf else None
    return {
        "trip_updates_sum": len(trips),
        "unique_vehicles": min(len(vehicles), VEHICLES_MAX),
        "unique_routes": len(routes),
        "unique_trips": len(trips),
        "unique_stops_served": min(len(stops), STOPS_MAX),
        "operators_active": sorted(operators),
        "probe_count": len(buf),
        "first_probe": first,
        "last_probe": last,
        "window_hours": ROLLING_HOURS,
    }


def write_motion_stats(window_stats):
    out = {
        "schema": "v2.0-rolling-window",
        "date": now_cyprus().date().isoformat(),
        "generated_at_utc": now_utc().isoformat(),
        "cyprus_stats_last6h": window_stats,
    }
    (DATA_DIR / "motion-stats.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2)
    )
    print(f"[motion-stats] trips={window_stats['unique_trips']} "
          f"vehicles={window_stats['unique_vehicles']} "
          f"stops={window_stats['unique_stops_served']} "
          f"operators={len(window_stats['operators_active'])} "
          f"probes={window_stats['probe_count']}")


# ─── KM HELPERS (haversine + shape lengths) ────────────────────────────────

def haversine_m(lat1, lon1, lat2, lon2):
    """Distance in meters between two WGS84 lat/lon points."""
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def compute_shape_lengths_from_zip(z):
    """Read shapes.txt from an open GTFS zip → {shape_id: total_distance_m}."""
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


# ─── PLANNED (GTFS static) ──────────────────────────────────────────────────

def gtfs_date_str(d):
    return d.strftime("%Y%m%d")


def regenerate_planned():
    today_local = now_cyprus().date()
    target = gtfs_date_str(today_local)

    total_trips = 0
    total_stops = set()
    operators_today = []
    hour_counter = {}
    trip_distances = {}    # trip_id → distance_m (today's planned trips)
    total_km_planned = 0.0

    for ag in AGENCY_IDS:
        zip_path = GTFS_STATIC_DIR / f"{ag}_google_transit.zip"
        if not zip_path.exists():
            print(f"[planned] missing {zip_path.name}, skipping")
            continue
        with zipfile.ZipFile(zip_path) as z:
            # service_ids active today
            services = set()
            with z.open("calendar_dates.txt") as f:
                for row in csv.DictReader(
                    (line.decode("utf-8-sig") for line in f)
                ):
                    if row["date"] == target and row["exception_type"] == "1":
                        services.add(row["service_id"])
            if not services:
                continue
            # trips of today + shape_id per trip
            today_trip_ids = set()
            trip_shape = {}
            with z.open("trips.txt") as f:
                for row in csv.DictReader(
                    (line.decode("utf-8-sig") for line in f)
                ):
                    if row["service_id"] in services:
                        tid = row["trip_id"]
                        today_trip_ids.add(tid)
                        sid = row.get("shape_id")
                        if sid:
                            trip_shape[tid] = sid
            if not today_trip_ids:
                continue
            operators_today.append(AGENCY_NAMES[ag])
            total_trips += len(today_trip_ids)
            # Shape lengths (compute only for this agency's shapes)
            shape_lengths = compute_shape_lengths_from_zip(z)
            for tid in today_trip_ids:
                sid = trip_shape.get(tid)
                if sid and sid in shape_lengths:
                    d_m = shape_lengths[sid]
                    trip_distances[tid] = d_m
                    total_km_planned += d_m / 1000.0
            # stops + hour of first departure
            trip_first_seq = {}
            with z.open("stop_times.txt") as f:
                for row in csv.DictReader(
                    (line.decode("utf-8-sig") for line in f)
                ):
                    tid = row["trip_id"]
                    if tid not in today_trip_ids:
                        continue
                    total_stops.add(row["stop_id"])
                    try:
                        seq = int(row.get("stop_sequence", "999"))
                    except ValueError:
                        continue
                    dep = row.get("departure_time") or row.get("arrival_time") or ""
                    cur = trip_first_seq.get(tid)
                    if cur is None or seq < cur[0]:
                        trip_first_seq[tid] = (seq, dep)
            for _, dep in trip_first_seq.values():
                try:
                    h = int(dep.split(":")[0]) % 24
                    hour_counter[h] = hour_counter.get(h, 0) + 1
                except Exception:
                    pass

    peak_hour = None
    peak_count = 0
    if hour_counter:
        peak_hour = max(hour_counter, key=hour_counter.get)
        peak_count = hour_counter[peak_hour]

    # Cache per-trip distances so daily-executed km calc doesn't reparse shapes
    TRIP_DIST_PATH.write_text(
        json.dumps(
            {"date": today_local.isoformat(), "trip_distances_m": trip_distances},
            ensure_ascii=False,
        )
    )

    # ── DAILY VERIFICATION GATE ────
    verified_km, verification = verify_planned_km(
        trip_distances, total_km_planned, total_trips, operators_today, today_local
    )

    payload = {
        "schema": "v1.2-planned",
        "date": today_local.isoformat(),
        "generated_at_utc": now_utc().isoformat(),
        "trips_planned": total_trips,
        "stops_planned": min(len(total_stops), STOPS_MAX),
        "vehicles_ceiling": VEHICLES_MAX,
        "km_planned": round(verified_km, 1),
        "km_planned_raw": round(total_km_planned, 1),
        "verification": {
            "verdict": verification["verdict"],
            "issues": verification["issues"],
            "verified_at_utc": verification["verified_at_utc"],
        },
        "operators_active": len(operators_today),
        "operators_list": operators_today,
        "peak_hour": peak_hour,
        "peak_count": peak_count,
        "hour_distribution": hour_counter,
    }
    (DATA_DIR / "planned.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    print(f"[planned] {today_local} → trips={total_trips} stops={len(total_stops)} "
          f"km={verified_km:.1f} (raw={total_km_planned:.1f}) "
          f"operators={len(operators_today)} peak={peak_hour}:00 ({peak_count}) "
          f"verdict={verification['verdict']}")


def verify_planned_km(trip_distances, total_km_planned_raw, n_trips, operators, date_local):
    """Daily verification gate. Cross-checks computed km against source data
    + historical baselines. If anomalies are detected, applies corrections.
    """
    issues = []
    corrections = []

    # Check 1 — sum consistency
    recomputed = sum(trip_distances.values()) / 1000.0
    delta_pct = 100.0 * abs(recomputed - total_km_planned_raw) / max(total_km_planned_raw, 1.0)
    if delta_pct > 0.1:
        issues.append(f"SUM_MISMATCH:{delta_pct:.3f}%")

    # Check 2 — trip count vs cache
    if len(trip_distances) != n_trips:
        missing = n_trips - len(trip_distances)
        if missing > 0:
            issues.append(f"MISSING_SHAPES:{missing}_of_{n_trips}")

    # Check 3 — zero-length trips (shape malformed)
    n_zero = sum(1 for d in trip_distances.values() if d <= 1.0)
    if n_zero > 0:
        issues.append(f"ZERO_DIST_TRIPS:{n_zero}")

    # Check 4 — implausibly long (>500 km in Cyprus is impossible)
    n_long = sum(1 for d in trip_distances.values() if d > 500_000)
    if n_long > 0:
        issues.append(f"IMPLAUSIBLY_LONG:{n_long}")

    # Check 5 — historical baseline (median of last 7 days)
    baseline_km = None
    baseline_dates = 0
    if DAILY_HISTORY_PATH.exists():
        try:
            hist = json.loads(DAILY_HISTORY_PATH.read_text())
            past_kms = [
                e.get("km_planned")
                for e in hist.get("entries", [])
                if isinstance(e.get("km_planned"), (int, float))
            ]
            if past_kms:
                recent = sorted(past_kms[-7:])
                baseline_km = recent[len(recent) // 2]
                baseline_dates = len(recent)
        except Exception:
            pass
    if baseline_km:
        delta = abs(total_km_planned_raw - baseline_km) / baseline_km
        if delta > 0.20:
            issues.append(
                f"DELTA_VS_BASELINE>20%:today={total_km_planned_raw:.0f}_baseline={baseline_km:.0f}"
            )

    # ── CORRECTIONS ────
    # Haversine point-to-point on shapes underestimates real driven km because
    # shape vertices are sparse along curves. Empirical correction factor for
    # Cyprus PT shape granularity ≈ 1.07.
    HAVERSINE_TO_ROAD = 1.07
    corrected = total_km_planned_raw * HAVERSINE_TO_ROAD
    corrections.append(f"haversine_to_road×{HAVERSINE_TO_ROAD}")

    if n_trips == 0 or total_km_planned_raw < 1.0:
        if baseline_km:
            corrected = baseline_km * HAVERSINE_TO_ROAD
            corrections.append(f"fell_back_to_baseline_{baseline_km}")
            issues.append("FELL_BACK_TO_BASELINE")
        else:
            corrections.append("no_baseline_available")

    verdict = "PASS" if not issues else ("WARN" if all(
        not i.startswith("SUM_MISMATCH") and not i.startswith("FELL_BACK") for i in issues
    ) else "FAIL")

    record = {
        "schema": "v1.0-verification-planned",
        "verified_at_utc": now_utc().isoformat(),
        "date": date_local.isoformat(),
        "trips_count": n_trips,
        "operators_count": len(operators),
        "km_raw_haversine": round(total_km_planned_raw, 1),
        "km_corrected_road_estimate": round(corrected, 1),
        "km_published": round(corrected, 1),
        "haversine_to_road_factor": HAVERSINE_TO_ROAD,
        "baseline_km_median_last7": baseline_km,
        "baseline_sample_size": baseline_dates,
        "issues": issues,
        "corrections_applied": corrections,
        "verdict": verdict,
    }
    (DATA_DIR / "verification_planned.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2)
    )
    print(f"[verify] verdict={verdict} raw={total_km_planned_raw:.1f} "
          f"corrected={corrected:.1f} issues={issues or '—'}")
    return corrected, record


def planned_needs_refresh():
    if PLANNED_REGEN == "always":
        return True
    p = DATA_DIR / "planned.json"
    if not p.exists():
        return True
    try:
        existing = json.loads(p.read_text())
        return existing.get("date") != now_cyprus().date().isoformat()
    except Exception:
        return True


# ─── DAILY EXECUTED ACCUMULATOR ────────────────────────────────────────────
#
# daily_executed.json — accumulates trip_ids observed today across every poll.
# At Cyprus-date rollover, the previous day's accumulator is finalized to
# daily_history.json (last HISTORY_KEEP_DAYS retained).

def load_trip_distances():
    if not TRIP_DIST_PATH.exists():
        return {}
    try:
        data = json.loads(TRIP_DIST_PATH.read_text())
        return data.get("trip_distances_m", {}) or {}
    except Exception:
        return {}


def empty_daily_executed(date_str):
    return {
        "schema": "v1.0-daily-executed",
        "date": date_str,
        "started_at_utc": now_utc().isoformat(),
        "last_updated_utc": now_utc().isoformat(),
        "poll_count": 0,
        "seen_trip_ids": [],
        "km_executed_estimated": 0.0,
    }


def append_to_history(prev):
    """Finalize prev day's accumulator → append to daily_history.json."""
    history = {"schema": "v1.0-daily-history", "entries": []}
    if DAILY_HISTORY_PATH.exists():
        try:
            history = json.loads(DAILY_HISTORY_PATH.read_text())
        except Exception:
            pass

    planned = {}
    p = DATA_DIR / "planned.json"
    if p.exists():
        try:
            planned = json.loads(p.read_text())
        except Exception:
            pass

    trips_executed = len(prev.get("seen_trip_ids", []))
    km_exec = round(prev.get("km_executed_estimated", 0.0), 1)
    trips_planned = planned.get("trips_planned")
    km_planned = planned.get("km_planned")
    executed_pct = None
    if trips_planned:
        executed_pct = round(100.0 * trips_executed / trips_planned, 1)

    entry = {
        "date": prev.get("date"),
        "trips_planned": trips_planned,
        "trips_executed": trips_executed,
        "km_planned": km_planned,
        "km_executed_estimated": km_exec,
        "executed_pct_estimate": executed_pct,
        "poll_count": prev.get("poll_count", 0),
        "finalized_at_utc": now_utc().isoformat(),
    }
    entries = [e for e in history.get("entries", []) if e.get("date") != entry["date"]]
    entries.append(entry)
    entries.sort(key=lambda e: e.get("date", ""))
    entries = entries[-HISTORY_KEEP_DAYS:]
    history["entries"] = entries
    history["schema"] = "v1.0-daily-history"
    DAILY_HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    print(f"[history] finalized {entry['date']}: trips={trips_executed}/"
          f"{trips_planned or '?'} km={km_exec}/{km_planned or '?'} "
          f"pct={executed_pct}")


def rollover_daily_executed():
    """If accumulator date != today Cyprus, finalize prev day and start fresh."""
    today = now_cyprus().date().isoformat()
    if not DAILY_EXEC_PATH.exists():
        DAILY_EXEC_PATH.write_text(
            json.dumps(empty_daily_executed(today), ensure_ascii=False, indent=2)
        )
        print(f"[rollover] initialized accumulator for {today}")
        return
    try:
        cur = json.loads(DAILY_EXEC_PATH.read_text())
    except Exception:
        DAILY_EXEC_PATH.write_text(
            json.dumps(empty_daily_executed(today), ensure_ascii=False, indent=2)
        )
        print(f"[rollover] reset corrupted accumulator → {today}")
        return
    if cur.get("date") == today:
        return
    # New day → finalize previous day's accumulator
    if cur.get("seen_trip_ids"):
        append_to_history(cur)
    DAILY_EXEC_PATH.write_text(
        json.dumps(empty_daily_executed(today), ensure_ascii=False, indent=2)
    )
    print(f"[rollover] {cur.get('date')} → {today}")


def update_daily_executed(snapshot):
    """Append observed trip_ids to today's accumulator + recompute km estimate.
    Also runs the executed-km verification gate: compares observed cumulative
    against EXPECTED cumulative by the current hour of day (from hour_distribution),
    not against the full-day total."""
    try:
        cur = json.loads(DAILY_EXEC_PATH.read_text())
    except Exception:
        cur = empty_daily_executed(now_cyprus().date().isoformat())

    seen = set(cur.get("seen_trip_ids", []))
    seen.update(snapshot.get("trips", []))
    distances = load_trip_distances()
    km = sum(distances.get(tid, 0.0) for tid in seen) / 1000.0

    cur["seen_trip_ids"] = sorted(seen)
    cur["km_executed_estimated"] = round(km, 1)
    cur["last_updated_utc"] = now_utc().isoformat()
    cur["poll_count"] = cur.get("poll_count", 0) + 1

    # ── EXECUTED VERIFICATION: observed vs expected-by-now (not full day) ──
    verification = verify_executed_km(len(seen), km)
    cur["progress"] = verification["progress"]
    cur["verification"] = {
        "verdict": verification["verdict"],
        "issues": verification["issues"],
        "verified_at_utc": verification["verified_at_utc"],
    }

    DAILY_EXEC_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2))
    print(f"[daily-exec] {cur['date']} polls={cur['poll_count']} "
          f"trips_seen={len(seen)} km_estimate={km:.1f} "
          f"vs_expected={verification['progress']['delivery_pct_vs_expected']}% "
          f"verdict={verification['verdict']}")
    return cur


def verify_executed_km(observed_trips, observed_km):
    """Compare cumulative observed (trips + km) against EXPECTED cumulative by
    the current Cyprus hour, derived from planned.hour_distribution. Writes an
    audit trail to verification_executed.json.

    Unlike comparing to full-day totals (which gives misleading low percentages
    early in the day), this measures actual delivery quality at any moment:
    'how close are we to where the schedule says we should be right now'.
    """
    now = now_cyprus()
    current_hour = now.hour
    today = now.date().isoformat()
    issues = []

    # Pull planned totals + hourly distribution
    planned_path = DATA_DIR / "planned.json"
    total_trips = 0
    total_km = 0.0
    hour_dist = {}
    if planned_path.exists():
        try:
            p = json.loads(planned_path.read_text())
            total_trips = p.get("trips_planned", 0) or 0
            total_km = p.get("km_planned", 0.0) or 0.0
            hour_dist = p.get("hour_distribution", {}) or {}
        except Exception:
            issues.append("PLANNED_READ_FAIL")

    # Cumulative trips scheduled to have STARTED by end of current_hour.
    # hour_distribution counts trips by their first-departure hour, so this
    # tracks "trips that should be in-flight or completed by now".
    cumulative_trips_expected = 0
    for h in range(0, current_hour + 1):
        cumulative_trips_expected += int(hour_dist.get(str(h), 0))

    # Pro-rata km expected: assume km is roughly proportional to trip count.
    # (Refinement possible later: per-hour shape-distance weighting.)
    expected_pct_of_day = (cumulative_trips_expected / total_trips) if total_trips else 0.0
    expected_km_by_now = total_km * expected_pct_of_day

    # Delivery ratios — what fraction of "expected by now" did we actually observe.
    delivery_ratio_trips = (
        observed_trips / cumulative_trips_expected if cumulative_trips_expected > 0 else 0.0
    )
    delivery_ratio_km = (
        observed_km / expected_km_by_now if expected_km_by_now > 0 else 0.0
    )
    delivery_pct_vs_expected = round(delivery_ratio_km * 100, 1)

    # Verdict thresholds (relative to expected, not full day)
    if cumulative_trips_expected > 0:
        if delivery_ratio_trips < 0.40:
            issues.append(f"BELOW_EXPECTED:{delivery_ratio_trips:.2f}")
        if delivery_ratio_trips > 1.60:
            issues.append(f"ABOVE_EXPECTED:{delivery_ratio_trips:.2f}")
    elif observed_trips > 0:
        # We observed trips but the schedule says none should have started yet
        # (e.g., before 5am). Flag as informational, not as error.
        issues.append("EARLY_OBSERVATIONS_BEFORE_SCHEDULE")

    if not total_trips:
        issues.append("NO_PLANNED_DATA")

    verdict = "PASS" if not issues else (
        "WARN" if all(
            not i.startswith("PLANNED_READ_FAIL") and not i.startswith("NO_PLANNED_DATA")
            for i in issues
        ) else "FAIL"
    )

    progress = {
        "cyprus_hour": current_hour,
        "expected_trips_by_now": cumulative_trips_expected,
        "expected_km_by_now": round(expected_km_by_now, 1),
        "expected_pct_of_day": round(expected_pct_of_day * 100, 1),
        "observed_trips": observed_trips,
        "observed_km": round(observed_km, 1),
        "delivery_ratio_trips": round(delivery_ratio_trips, 3),
        "delivery_ratio_km": round(delivery_ratio_km, 3),
        "delivery_pct_vs_expected": delivery_pct_vs_expected,
    }

    record = {
        "schema": "v1.0-verification-executed",
        "verified_at_utc": now_utc().isoformat(),
        "date": today,
        "progress": progress,
        "issues": issues,
        "verdict": verdict,
    }
    (DATA_DIR / "verification_executed.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2)
    )
    return record


# ─── MAIN ──────────────────────────────────────────────────────────────────

def main():
    errors = []
    # 0. Rollover daily executed accumulator if Cyprus date changed
    try:
        rollover_daily_executed()
    except Exception as e:
        print(f"[ERROR] rollover failed: {e}", file=sys.stderr)
        errors.append(("rollover", str(e)))

    # 1. Hero A (planned today) — must run first so trip_distances cache is ready
    if planned_needs_refresh():
        try:
            regenerate_planned()
        except Exception as e:
            print(f"[ERROR] planned regen failed: {e}", file=sys.stderr)
            errors.append(("planned", str(e)))
    else:
        print("[planned] already current for today, skipping")

    # 2. Hero B (executed rolling window) + daily executed accumulator
    try:
        feed = fetch_feed()
        snap = snapshot_from_feed(feed)
        buf = update_rolling(snap)
        window_stats = aggregate_window(buf)
        write_motion_stats(window_stats)
        update_daily_executed(snap)
    except Exception as e:
        print(f"[ERROR] executed refresh failed: {e}", file=sys.stderr)
        errors.append(("executed", str(e)))

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
