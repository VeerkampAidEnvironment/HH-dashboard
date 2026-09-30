"""Build map features from saved follow-up plot tracks and household assessments."""

import json
import math
from statistics import median


def _distance_metres(first, second):
    """Great-circle distance between [longitude, latitude] GPS fixes."""
    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    half_lat = math.sin((lat2 - lat1) / 2) ** 2
    half_lon = math.sin((lon2 - lon1) / 2) ** 2
    arc = half_lat + math.cos(lat1) * math.cos(lat2) * half_lon
    return 6371000 * 2 * math.atan2(math.sqrt(arc), math.sqrt(max(0, 1 - arc)))


def _clean_plot_points(points):
    coordinates = []
    for point in points:
        try:
            latitude = float(point["latitude"])
            longitude = float(point["longitude"])
            accuracy = float(point.get("accuracy", 0))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        if (not all(math.isfinite(value) for value in (latitude, longitude, accuracy))
                or not -90 <= latitude <= 90 or not -180 <= longitude <= 180
                or accuracy < 0 or accuracy > 100):
            continue
        coordinate = [longitude, latitude]
        if not coordinates or coordinates[-1] != coordinate:
            coordinates.append(coordinate)
    if len(coordinates) < 2:
        return coordinates

    steps = [_distance_metres(a, b) for a, b in zip(coordinates, coordinates[1:])]
    ordinary_steps = [step for step in steps if step <= 150]
    typical_step = median(ordinary_steps) if ordinary_steps else 0
    # Tracks are taken continuously while walking. A much larger step is
    # probably a GPS jump; cap the threshold even when the track is sparse.
    limit = min(500, max(150, typical_step * 12))
    cleaned = []
    for index, point in enumerate(coordinates):
        if 0 < index < len(coordinates) - 1:
            before = _distance_metres(coordinates[index - 1], point)
            after = _distance_metres(point, coordinates[index + 1])
            across = _distance_metres(coordinates[index - 1], coordinates[index + 1])
            if before > 75 and after > 75 and across < min(before, after) / 3:
                continue
        cleaned.append(point)

    segments = [[cleaned[0]]]
    for point in cleaned[1:]:
        if _distance_metres(segments[-1][-1], point) > limit:
            segments.append([])
        segments[-1].append(point)
    longest = max(segments, key=len)
    # Multiple disconnected single fixes cannot describe a reliable plot.
    return longest if len(longest) > 1 or len(cleaned) == 1 else []


def plot_geometry(answers_json):
    try:
        answers = json.loads(answers_json)
        track = answers.get("a7_gps")
        track = track.get("answer", track) if isinstance(track, dict) else track
        points = track.get("points") if isinstance(track, dict) else None
        if not isinstance(points, list) or not 1 <= len(points) <= 5000:
            return None
        coordinates = _clean_plot_points(points)
        if len(coordinates) >= 3:
            return {"type": "Polygon", "coordinates": [coordinates + [coordinates[0]]]}
        if len(coordinates) == 2:
            return {"type": "LineString", "coordinates": coordinates}
        if coordinates:
            return {"type": "Point", "coordinates": coordinates[0]}
    except (TypeError, ValueError, KeyError, AttributeError):
        pass
    return None


def beneficiary_features(connection, cbf_name=None):
    """One feature per AE beneficiary, using their latest visit with a GPS track."""
    where = "AND r.cbf_name=?" if cbf_name is not None else ""
    rows = connection.execute(f"""
        SELECT r.id, r.name, r.village, r.cbf_name, f.uid, e.event_date,
               e.id AS event_id, fr.answers, a.rvo_passed, a.project_passed,
               a.household_outcome
        FROM followup_responses fr
        JOIN field_event_entries ee ON ee.id=fr.event_entry_id
        JOIN field_events e ON e.id=ee.event_id AND e.event_type='followup'
        JOIN records r ON r.id=fr.record_id AND r.dataset='training' AND r.archived_at IS NULL
        JOIN farmers f ON f.id=r.farmer_id
        LEFT JOIN followup_assessments a ON a.event_id=e.id
        WHERE 1=1 {where}
        ORDER BY e.event_date DESC, e.id DESC, fr.id DESC
    """, (cbf_name,) if cbf_name is not None else ()).fetchall()
    received = {
        row["record_id"]: row["count"] for row in connection.execute("""
            SELECT record_id, COUNT(*) AS count FROM topic_statuses
            WHERE training_received=1 GROUP BY record_id
        """).fetchall()
    }
    features = []
    seen = set()
    for row in rows:
        if row["id"] in seen:
            continue
        geometry = plot_geometry(row["answers"])
        if geometry is None:
            continue
        seen.add(row["id"])
        if row["rvo_passed"] is None or row["project_passed"] is None:
            status = "unassessed"
        else:
            status = "validated" if row["rvo_passed"] and row["project_passed"] else "failed"
        features.append({
            "type": "Feature", "geometry": geometry,
            "properties": {
                "record_id": row["id"], "name": row["name"], "uid": row["uid"],
                "village": row["village"], "cbf": row["cbf_name"],
                "date": row["event_date"], "status": status,
                "rvo_passed": row["rvo_passed"], "project_passed": row["project_passed"],
                "outcome": row["household_outcome"],
                "training_count": received.get(row["id"], 0),
            },
        })
    return {"type": "FeatureCollection", "features": features}
