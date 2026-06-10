#!/usr/bin/env python3
"""One-off diagnostic: vehicle identifier coverage in the live GTFS-RT feed.

Prints to stdout only. Writes NOTHING to data/. Safe to run anytime.
Checks: id/label/license_plate coverage, uniqueness, id<->plate 1:1 mapping.
"""
import os
import sys
from collections import Counter, defaultdict

import requests
from google.transit import gtfs_realtime_pb2

FEED_URL = os.environ.get(
    "MOTION_FEED_URL", "http://20.19.98.194:8328/Api/api/gtfs-realtime"
)


def main():
    print(f"[diag] fetching {FEED_URL}")
    r = requests.get(FEED_URL, timeout=30)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    print(f"[diag] feed timestamp: {feed.header.timestamp}")

    vps = [e.vehicle for e in feed.entity if e.HasField("vehicle")]
    print(f"[diag] VehiclePosition entities: {len(vps)}")

    cov = Counter()
    id_to_plates = defaultdict(set)
    plate_to_ids = defaultdict(set)
    label_eq_plate = 0
    for vp in vps:
        v = vp.vehicle
        if v.id:
            cov["id"] += 1
        if v.label:
            cov["label"] += 1
        if v.license_plate:
            cov["license_plate"] += 1
        if v.id and v.license_plate:
            id_to_plates[v.id].add(v.license_plate)
            plate_to_ids[v.license_plate].add(v.id)
        if v.label and v.label == v.license_plate:
            label_eq_plate += 1

    n = len(vps)
    print(f"[diag] coverage: id {cov['id']}/{n} | label {cov['label']}/{n} "
          f"| license_plate {cov['license_plate']}/{n}")
    print(f"[diag] label == license_plate in {label_eq_plate}/{n}")

    ids = [vp.vehicle.id for vp in vps if vp.vehicle.id]
    plates = [vp.vehicle.license_plate for vp in vps if vp.vehicle.license_plate]
    print(f"[diag] unique ids: {len(set(ids))}/{len(ids)} | "
          f"unique plates: {len(set(plates))}/{len(plates)}")

    multi_plate = {k: v for k, v in id_to_plates.items() if len(v) > 1}
    multi_id = {k: v for k, v in plate_to_ids.items() if len(v) > 1}
    print(f"[diag] ids mapping to >1 plate: {len(multi_plate)}")
    print(f"[diag] plates mapping to >1 id: {len(multi_id)}")
    for k, v in list(multi_plate.items())[:5]:
        print(f"[diag]   id {k} -> plates {sorted(v)}")
    for k, v in list(multi_id.items())[:5]:
        print(f"[diag]   plate {k} -> ids {sorted(v)}")

    print("[diag] sample (first 5): id | label | plate | route_id")
    for vp in vps[:5]:
        v = vp.vehicle
        rid = vp.trip.route_id if vp.HasField("trip") else "-"
        print(f"[diag]   {v.id} | {v.label} | {v.license_plate} | {rid}")

    # TripUpdate-side vehicle ids (we also dedup on these in production)
    tu_ids = {
        e.trip_update.vehicle.id
        for e in feed.entity
        if e.HasField("trip_update") and e.trip_update.vehicle.id
    }
    vp_ids = set(ids)
    print(f"[diag] TripUpdate vehicle ids: {len(tu_ids)} "
          f"(not in VehiclePositions: {len(tu_ids - vp_ids)})")
    print("[diag] done — no files written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
