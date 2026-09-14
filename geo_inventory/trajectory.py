from __future__ import annotations

import math
from typing import Any


def minimum_curvature(
    stations: list[dict[str, Any]], kb_elevation: float | None = None
) -> list[dict[str, float]]:
    """Convert MD/inc/azi to TVD and offsets using the minimum-curvature method.

    TVDSS is positive downward below mean sea level: TVD - KB elevation.
    z_msl is the corresponding elevation coordinate: KB elevation - TVD.
    """
    if not stations:
        return []
    clean = sorted(
        ({"md": float(s["md"]), "inclination": float(s["inclination"]), "azimuth": float(s["azimuth"])} for s in stations),
        key=lambda s: s["md"],
    )
    deduped: list[dict[str, float]] = []
    for station in clean:
        if station["md"] < 0 or not 0 <= station["inclination"] <= 180:
            raise ValueError("MD 必须非负且井斜角必须在 0–180° 之间")
        if deduped and station["md"] == deduped[-1]["md"]:
            deduped[-1] = station
        else:
            deduped.append(station)

    first = deduped[0]
    inc0 = math.radians(first["inclination"])
    azi0 = math.radians(first["azimuth"] % 360)
    tvd = first["md"] * math.cos(inc0)
    northing = first["md"] * math.sin(inc0) * math.cos(azi0)
    easting = first["md"] * math.sin(inc0) * math.sin(azi0)
    result = [_station_result(first, tvd, northing, easting, kb_elevation)]

    for previous, current in zip(deduped, deduped[1:]):
        delta_md = current["md"] - previous["md"]
        i1, i2 = math.radians(previous["inclination"]), math.radians(current["inclination"])
        a1, a2 = math.radians(previous["azimuth"] % 360), math.radians(current["azimuth"] % 360)
        cos_dogleg = max(-1.0, min(1.0, math.cos(i1) * math.cos(i2) + math.sin(i1) * math.sin(i2) * math.cos(a2 - a1)))
        dogleg = math.acos(cos_dogleg)
        ratio = 1.0 if dogleg < 1e-10 else 2.0 / dogleg * math.tan(dogleg / 2.0)
        tvd += delta_md / 2.0 * (math.cos(i1) + math.cos(i2)) * ratio
        northing += delta_md / 2.0 * (math.sin(i1) * math.cos(a1) + math.sin(i2) * math.cos(a2)) * ratio
        easting += delta_md / 2.0 * (math.sin(i1) * math.sin(a1) + math.sin(i2) * math.sin(a2)) * ratio
        result.append(_station_result(current, tvd, northing, easting, kb_elevation))
    return result


def _station_result(station: dict[str, float], tvd: float, northing: float, easting: float, kb: float | None) -> dict[str, float]:
    row = {**station, "tvd": tvd, "northing": northing, "easting": easting}
    if kb is not None:
        row["tvdss"] = tvd - kb
        row["z_msl"] = kb - tvd
    return row

