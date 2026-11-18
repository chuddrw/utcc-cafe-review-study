from __future__ import annotations

import math
from typing import Any

from shapely.geometry import Point, Polygon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _poly(coords: list[list[float]]) -> Polygon:
    # config stores [lat, lon]; shapely wants (lon, lat)
    return Polygon([(lon, lat) for lat, lon in coords])


def point_in_polygon(lat: float, lon: float, coords: list[list[float]]) -> bool:
    return _poly(coords).covers(Point(lon, lat))


def classify_area(lat: float, lon: float, geography: dict[str, Any]) -> str:
    if not point_in_polygon(lat, lon, geography["toronto_polygon"]):
        return "outside_toronto"
    if point_in_polygon(lat, lon, geography["downtown_polygon"]):
        return "downtown"
    # nearest suburban label if within ~4 km of a documented centre, else other
    best_name = None
    best_d = float("inf")
    for area in geography["suburban_areas"]:
        d = haversine_m(lat, lon, area["lat"], area["lon"])
        if d < best_d:
            best_d = d
            best_name = area["name"]
    if best_d <= 4000:
        return f"suburban:{best_name}"
    return "other"


def area_bucket(area: str) -> str:
    if area == "downtown":
        return "downtown"
    if area.startswith("suburban:"):
        return "suburban"
    if area == "outside_toronto":
        return "outside_toronto"
    return "other"


def university_flag(lat: float, lon: float, geography: dict[str, Any], radius_m: float) -> tuple[bool, str | None, float | None]:
    best = None
    best_d = float("inf")
    for campus in geography["campuses"]:
        d = haversine_m(lat, lon, campus["lat"], campus["lon"])
        if d < best_d:
            best_d = d
            best = campus["name"]
    if best is not None and best_d <= radius_m:
        return True, best, best_d
    return False, None, None


def infer_format(place: dict[str, Any]) -> str:
    """Heuristic format label from metadata only; unknown stays unknown."""
    blob = " ".join(
        str(place.get(k) or "")
        for k in ("name", "category", "type", "subtypes", "description", "located_in")
    ).lower()
    if any(x in blob for x in ("airport", "terminal")):
        return "airport"
    if any(x in blob for x in ("hospital", "medical centre", "medical center")):
        return "hospital"
    if "kiosk" in blob:
        return "kiosk"
    if "drive" in blob and "through" in blob.replace("-", " "):
        return "drive_through"
    if any(x in blob for x in ("shopping mall", "mall food court", "inside")) and "cafe" not in blob:
        # located_in mall alone is not enough to force kiosk
        if "food court" in blob:
            return "mall_food_court"
    if any(x in blob for x in ("coffee shop", "cafe", "café", "coffeehouse")):
        return "cafe"
    return "unknown"
