#!/usr/bin/env python3
"""
Daily planned-vs-executed analytics for inframind.eu — v2.0.

Runs hourly (GitHub Actions). Consumes artifacts already produced by the
5-minute refresh_motion_data.py pipeline — NO extra load on the MOTION
endpoint:

  data/_rolling_snapshots.json  — RT probes, last 6h (trip/stop/vehicle ids)
  data/_today_schedule.json     — today's planned trips (first-dep, km, stops)

Produces (schemas compatible with the retired verify_cyprus_wide.py, bumped
to v2.0 with a history_note marking the 2026-08-24 reset):

  data/daily_executed.json        — intra-day accumulator (current Cyprus day)
  data/daily_history.json         — one finalized entry per completed day
  data/verification_planned.json  — today's planned totals vs 7-day baseline
  data/motion-feed-health.json    — per-agency stop coverage (from today's probes)

History note: entries before 2026-08-24 were produced by a different tool
during the GTFS-static freeze (2026-06-02 → 2026-08-07) and are archived in
data/archive/pre-fix/ — they understate planned figures and must not be
compared with v2.0 data.

Dependencies: Python stdlib only.
"""
import csv
import json
import statistics
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
GTFS_DIR = REPO_ROOT / "gtfs"

ROLLING_PATH = DATA_DIR / "_rolling_snapshots.json"
SCHED_PATH = DATA_DIR / "_today_schedule.json"
EXEC_PATH = DATA_DIR / "daily_executed.json"
HIST_PATH = DATA_DIR / "daily_history.json"
VERIF_PATH = DATA_DIR / "verification_planned.json"
HEALTH_PATH = DATA_DIR / "motion-feed-health.json"

HISTORY_NOTE = (
    "History reset 2026-08-24. Pre-fix data (GTFS static frozen "
    "2026-06-02..2026-08-07, planned figures understated) archived in "
    "data/archive/pre-fix/. Not comparable with v2.0 entries."
)
PRODUCER = "INFRAMIND LTD · daily_analytics.py"
CONTACT = "ipanagiotidis@me.com"
FEED_SOURCE = "http://20.19.98.194:8328/Api/api/gtfs-realtime"
HAVERSINE_TO_ROAD = 1.07
HISTORY_MAX_ENTRIES = 400

AGENCY_META = {
    "2": ("ΟΣΥΠΑ", "Πάφος"),
    "4": ("ΟΣΕΑ", "Αμμόχωστος"),
    "5": ("INTERCITY", "Διαπεριφερειακά"),
    "6": ("ΕΜΕΛ", "Λεμεσός"),
    "9": ("NPT", "Λευκωσία"),
    "10": ("LPT", "Λάρνακα"),
    "11": ("PAME EXPRESS", "Express"),
}


def now_utc():
    return datetime.now(timezone.utc)


def to_cyprus(dt_utc):
    """EEST approximation (UTC+3), same convention as refresh_motion_data.py."""
    return dt_utc + timedelta(hours=3)


def cyprus_today():
    return to_cyprus(now_utc()).date().isoformat()


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def dump_json(path, obj, indent=1):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=indent))


# ─── Probe harvesting ──────────────────────────────────────────────────────
def todays_probes(buf, today):
    """Probes from the rolling buffer whose Cyprus-local date == today."""
    out = []
    for snap in buf:
        try:
            ts = datetime.fromisoformat(snap["ts_utc"])
        except Exception:
            continue
        if to_cyprus(ts).date().isoformat() == today:
            out.append(snap)
    return out


# ─── daily_executed accumulator ────────────────────────────────────────────
def fresh_state(today, planned_trips, planned_km):
    return {
        "schema": "v2.0-daily-executed",
        "history_note": HISTORY_NOTE,
        "date": today,
        "started_at_utc": now_utc().isoformat(),
        "last_updated_utc": now_utc().isoformat(),
        "poll_count": 0,
        "planned_trips": planned_trips,
        "planned_km": planned_km,
        "seen_trip_ids": [],
        "seen_probe_ts": [],
        "km_executed_estimated": 0.0,
        "trips_matched": 0,
        "trips_unmatched": 0,
        "progress": {},
        "verification": {},
    }


def finalize_into_history(state):
    """Append a finalized entry for state's date into daily_history.json."""
    if not state or state.get("poll_count", 0) == 0:
        return
    hist = load_json(HIST_PATH, {})
    if hist.get("schema") != "v2.0-daily-history":
        hist = {
            "schema": "v2.0-daily-history",
            "history_note": HISTORY_NOTE,
            "entries": [],
        }
    if any(e.get("date") == state["date"] for e in hist["entries"]):
        return
    planned_trips = state.get("planned_trips") or 0
    planned_km = state.get("planned_km") or 0.0
    executed = len(state.get("seen_trip_ids", []))
    km_exec = state.get("km_executed_estimated", 0.0)
    ratio = round(executed / planned_trips, 3) if planned_trips else None
    if ratio is None:
        verdict, issues = "NO_BASELINE", ["PLANNED_UNAVAILABLE"]
    elif ratio < 0.6:
        verdict, issues = "WARN", [f"BELOW_EXPECTED:{ratio:.2f}"]
    elif ratio > 1.2:
        verdict, issues = "WARN", [f"ABOVE_PLANNED:{ratio:.2f}"]
    else:
        verdict, issues = "OK", []
    hist["entries"].append({
        "date": state["date"],
        "trips_planned": planned_trips,
        "trips_executed": executed,
        "trips_matched": state.get("trips_matched", 0),
        "trips_unmatched": state.get("trips_unmatched", 0),
        "km_planned": round(planned_km, 1),
        "km_executed_estimated": round(km_exec, 1),
        "executed_pct_estimate": round(100.0 * ratio, 1) if ratio is not None else None,
        "delivery_ratio_trips": ratio,
        "poll_count": state.get("poll_count", 0),
        "verdict": verdict,
        "issues": issues,
        "finalized_at_utc": now_utc().isoformat(),
    })
    hist["entries"] = hist["entries"][-HISTORY_MAX_ENTRIES:]
    dump_json(HIST_PATH, hist)
    print(f"[history] finalized {state['date']}: {executed}/{planned_trips} "
          f"trips · verdict {verdict}")


def update_executed(state, probes, schedule):
    trips = schedule.get("trips", {}) if schedule else {}
    seen = set(state.get("seen_trip_ids", []))
    seen_ts = set(state.get("seen_probe_ts", []))
    new_polls = 0
    for snap in probes:
        ts = snap.get("ts_utc")
        if ts in seen_ts:
            continue
        seen_ts.add(ts)
        new_polls += 1
        seen.update(snap.get("trips", []))
    matched = [tid for tid in seen if tid in trips]
    unmatched = len(seen) - len(matched)
    km_exec = sum(trips[tid].get("km", 0.0) for tid in matched)

    state["last_updated_utc"] = now_utc().isoformat()
    state["poll_count"] = state.get("poll_count", 0) + new_polls
    state["seen_trip_ids"] = sorted(seen)
    state["seen_probe_ts"] = sorted(seen_ts)[-600:]
    state["km_executed_estimated"] = round(km_exec, 1)
    state["trips_matched"] = len(matched)
    state["trips_unmatched"] = unmatched

    # Intra-day progress vs schedule
    now_cy = to_cyprus(now_utc())
    now_sec = now_cy.hour * 3600 + now_cy.minute * 60 + now_cy.second
    exp_trips, exp_km = 0, 0.0
    tot_trips, tot_km = 0, 0.0
    for t in trips.values():
        dep = t.get("first_dep_sec")
        km = t.get("km", 0.0)
        tot_trips += 1
        tot_km += km
        if dep is not None and dep <= now_sec:
            exp_trips += 1
            exp_km += km
    ratio = round(len(seen) / exp_trips, 3) if exp_trips else None
    state["planned_trips"] = tot_trips or state.get("planned_trips")
    state["planned_km"] = round(tot_km, 1) if tot_km else state.get("planned_km")
    state["progress"] = {
        "cyprus_hour": now_cy.hour,
        "expected_trips_by_now": exp_trips,
        "expected_km_by_now": round(exp_km, 1),
        "expected_pct_of_day": round(100.0 * exp_trips / tot_trips, 1) if tot_trips else None,
        "observed_trips": len(seen),
        "observed_km": round(km_exec, 1),
        "delivery_ratio_trips": ratio,
        "delivery_ratio_km": round(km_exec / exp_km, 3) if exp_km else None,
    }
    issues = []
    verdict = "OK"
    if ratio is not None and now_cy.hour >= 9 and ratio < 0.5:
        verdict = "WARN"
        issues.append(f"BELOW_EXPECTED:{ratio:.2f}")
    state["verification"] = {
        "verdict": verdict,
        "issues": issues,
        "verified_at_utc": now_utc().isoformat(),
    }
    print(f"[executed] {state['date']}: {len(seen)} trips seen "
          f"({len(matched)} matched, {unmatched} unmatched) · "
          f"{km_exec:.0f} km · +{new_polls} polls")
    return state


# ─── verification_planned ──────────────────────────────────────────────────
def write_verification(today, schedule):
    trips = schedule.get("trips", {}) if schedule else {}
    if not trips:
        return
    km_raw = sum(t.get("km", 0.0) for t in trips.values())
    operators = sorted({t.get("agency") for t in trips.values() if t.get("agency")})
    hist = load_json(HIST_PATH, {})
    baseline_kms = [
        e["km_planned"] for e in hist.get("entries", [])[-7:]
        if isinstance(e.get("km_planned"), (int, float)) and e["km_planned"] > 0
    ]
    baseline = round(statistics.median(baseline_kms), 1) if baseline_kms else None
    issues, corrections = [], [f"haversine_to_road×{HAVERSINE_TO_ROAD}"]
    verdict = "OK"
    if baseline:
        delta = abs(km_raw - baseline) / baseline
        if delta > 0.2:
            verdict = "WARN"
            issues.append(
                f"DELTA_VS_BASELINE>20%:today={km_raw:.0f}_baseline={baseline:.0f}"
            )
    elif len(baseline_kms) == 0:
        verdict = "BASELINE_BUILDING"
        issues.append("NO_BASELINE_YET:history_reset_2026-08-24")
    km_road = round(km_raw * HAVERSINE_TO_ROAD, 1)
    dump_json(VERIF_PATH, {
        "schema": "v2.0-verification-planned",
        "history_note": HISTORY_NOTE,
        "verified_at_utc": now_utc().isoformat(),
        "date": today,
        "trips_count": len(trips),
        "operators_count": len(operators),
        "operators_list": operators,
        "km_raw_haversine": round(km_raw, 1),
        "km_corrected_road_estimate": km_road,
        "km_published": km_road,
        "haversine_to_road_factor": HAVERSINE_TO_ROAD,
        "baseline_km_median_last7": baseline,
        "baseline_sample_size": len(baseline_kms),
        "issues": issues,
        "corrections_applied": corrections,
        "verdict": verdict,
    })
    print(f"[verification] {len(trips)} trips · {km_road:.0f} km road-est · "
          f"baseline n={len(baseline_kms)} · verdict {verdict}")


# ─── motion-feed-health (per-agency stop coverage) ─────────────────────────
def load_static_stops():
    """agency_id → set(stop_id) from each GTFS zip's stops.txt."""
    out = {}
    for ag in AGENCY_META:
        zp = GTFS_DIR / f"{ag}_google_transit.zip"
        if not zp.exists():
            continue
        stops = set()
        try:
            with zipfile.ZipFile(zp) as z, z.open("stops.txt") as f:
                for row in csv.DictReader(
                    (line.decode("utf-8-sig") for line in f)
                ):
                    sid = (row.get("stop_id") or "").strip()
                    if sid:
                        stops.add(sid)
        except Exception as e:
            print(f"[health] skip agency {ag}: {e}")
            continue
        out[ag] = stops
    return out


def write_feed_health(today, probes, schedule):
    if not probes:
        return
    live_stops = set()
    live_trips = set()
    live_vehicles = set()
    live_routes = set()
    for snap in probes:
        live_stops.update(snap.get("stops", []))
        live_trips.update(snap.get("trips", []))
        live_vehicles.update(snap.get("vehicles", []))
        live_routes.update(snap.get("routes", []))
    static_stops = load_static_stops()
    all_static = set().union(*static_stops.values()) if static_stops else set()
    orphans = sorted(live_stops - all_static)
    per_agency = {}
    for ag, stops in static_stops.items():
        name, region = AGENCY_META[ag]
        live = live_stops & stops
        per_agency[ag] = {
            "agency_name": name,
            "region": region,
            "stops_total": len(stops),
            "stops_live": len(live),
            "stops_idle": len(stops) - len(live),
            "coverage_pct": round(100.0 * len(live) / len(stops), 1) if stops else 0.0,
        }
    last_ts = max(p["ts_utc"] for p in probes)
    covered = len(live_stops & all_static)
    dump_json(HEALTH_PATH, {
        "$schema_version": "2.0",
        "$producer": PRODUCER,
        "$contact": CONTACT,
        "history_note": HISTORY_NOTE,
        "generated_at": now_utc().isoformat(),
        "report_date": today,
        "feed_source": FEED_SOURCE,
        "feed_health": {
            "reachable": True,
            "probe_count_today": len(probes),
            "last_probe_utc": last_ts,
            "unique_trips_today": len(live_trips),
            "unique_vehicles_today": len(live_vehicles),
            "unique_routes_today": len(live_routes),
        },
        "coverage": {
            "stops_in_static_catalog": len(all_static),
            "stops_with_live_updates": covered,
            "stops_idle_or_off_service": len(all_static) - covered,
            "stops_orphan_in_feed_not_in_catalog": len(orphans),
            "coverage_pct": round(100.0 * covered / len(all_static), 1) if all_static else 0.0,
        },
        "per_agency": per_agency,
        "anomalies": {
            "orphan_stops_in_feed": {
                "count": len(orphans),
                "description": (
                    "Stops appearing in GTFS-RT feed but missing from static "
                    "GTFS catalog. Possible new stops not yet propagated to "
                    "static bundles."
                ),
                "sample_stop_ids": orphans[:20],
            }
        },
        "notes": {
            "methodology": (
                "Aggregated from the 5-minute pipeline's rolling RT probes — "
                "accumulated over the Cyprus service day. No additional calls "
                "to the MOTION endpoint."
            ),
            "frequency": "hourly (GitHub Actions), zero extra feed load",
        },
    })
    print(f"[health] coverage {covered}/{len(all_static)} stops · "
          f"{len(orphans)} orphans · {len(probes)} probes today")


# ─── main ──────────────────────────────────────────────────────────────────
def main():
    today = cyprus_today()
    buf = load_json(ROLLING_PATH, [])
    schedule = load_json(SCHED_PATH, None)
    if schedule and schedule.get("date") != today:
        print(f"[warn] _today_schedule date {schedule.get('date')} != {today} "
              "— planned figures unavailable this run")
        schedule = None

    state = load_json(EXEC_PATH, None)
    if not isinstance(state, dict) or state.get("schema") != "v2.0-daily-executed":
        state = None
    if state and state.get("date") != today:
        finalize_into_history(state)
        state = None
    if state is None:
        p_trips = len(schedule["trips"]) if schedule else None
        p_km = (round(sum(t.get("km", 0.0) for t in schedule["trips"].values()), 1)
                if schedule else None)
        state = fresh_state(today, p_trips, p_km)
        print(f"[executed] new day {today} (planned: {p_trips} trips)")

    probes = todays_probes(buf, today)
    state = update_executed(state, probes, schedule)
    dump_json(EXEC_PATH, state)

    if schedule:
        write_verification(today, schedule)
    write_feed_health(today, probes, schedule)
    print("[done]")


if __name__ == "__main__":
    main()
