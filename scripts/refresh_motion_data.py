#!/usr/bin/env python3
"""
Refresh MOTION ITS data for inframind.eu website.

Runs in GitHub Actions on cron (every 2 hours). Produces:

  website/data/motion-stats.json  — Hero B (executed last ~6h rolling window)
  website/data/planned.json       — Hero A (planned today from GTFS static)
  website/data/_rolling_snapshots.json  — internal rolling buffer (committed for state)

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
    # Infer operators from route_id prefix (first digits = agency id in Cyprus GTFS)
    for rid in routes:
        for ag in sorted(AGENCY_IDS, key=lambda x: -len(x)):
            if rid.startswith(ag) and AGENCY_NAMES.get(ag):
                operators.add(AGENCY_NAMES[ag])
                break
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
    """Union across rolling snapshots → rolling-window stats."""
    trips, vehicles, stops, routes, operators = set(), set(), set(), set(), set()
    for s in buf:
        trips.update(s.get("trips", []))
        vehicles.update(s.get("vehicles", []))
        stops.update(s.get("stops", []))
        routes.update(s.get("routes", []))
        operators.update(s.get("operators", []))
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
            # trips of today
            today_trip_ids = set()
            with z.open("trips.txt") as f:
                for row in csv.DictReader(
                    (line.decode("utf-8-sig") for line in f)
                ):
                    if row["service_id"] in services:
                        today_trip_ids.add(row["trip_id"])
            if not today_trip_ids:
                continue
            operators_today.append(AGENCY_NAMES[ag])
            total_trips += len(today_trip_ids)
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

    payload = {
        "schema": "v1.0-planned",
        "date": today_local.isoformat(),
        "generated_at_utc": now_utc().isoformat(),
        "trips_planned": total_trips,
        "stops_planned": min(len(total_stops), STOPS_MAX),
        "vehicles_ceiling": VEHICLES_MAX,
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
          f"operators={len(operators_today)} peak={peak_hour}:00 ({peak_count})")


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


# ─── MAIN ──────────────────────────────────────────────────────────────────

def main():
    errors = []
    # Hero B (executed rolling window)
    try:
        feed = fetch_feed()
        snap = snapshot_from_feed(feed)
        buf = update_rolling(snap)
        window_stats = aggregate_window(buf)
        write_motion_stats(window_stats)
    except Exception as e:
        print(f"[ERROR] executed refresh failed: {e}", file=sys.stderr)
        errors.append(("executed", str(e)))

    # Hero A (planned today)
    if planned_needs_refresh():
        try:
            regenerate_planned()
        except Exception as e:
            print(f"[ERROR] planned regen failed: {e}", file=sys.stderr)
            errors.append(("planned", str(e)))
    else:
        print("[planned] already current for today, skipping")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
