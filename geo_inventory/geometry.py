from __future__ import annotations

from collections.abc import Iterable


Point = tuple[float, float]


def point_in_ring(x: float, y: float, ring: list[list[float]] | list[Point]) -> bool:
    """Ray casting with boundary points treated as inside."""
    inside = False
    if len(ring) < 3:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        cross = (x - xi) * (yj - yi) - (y - yi) * (xj - xi)
        if abs(cross) < 1e-9 and min(xi, xj) - 1e-9 <= x <= max(xi, xj) + 1e-9 and min(yi, yj) - 1e-9 <= y <= max(yi, yj) + 1e-9:
            return True
        if (yi > y) != (yj > y):
            x_intersection = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x <= x_intersection:
                inside = not inside
        j = i
    return inside


def point_in_polygon(x: float, y: float, coordinates: list) -> bool:
    if not coordinates or not point_in_ring(x, y, coordinates[0]):
        return False
    return not any(point_in_ring(x, y, hole) for hole in coordinates[1:])


def polygon_area(coordinates: list) -> float:
    def ring_area(ring: list) -> float:
        return abs(sum(
            float(ring[i][0]) * float(ring[(i + 1) % len(ring)][1])
            - float(ring[(i + 1) % len(ring)][0]) * float(ring[i][1])
            for i in range(len(ring))
        )) / 2 if len(ring) >= 3 else 0.0

    if not coordinates:
        return 0.0
    return max(0.0, ring_area(coordinates[0]) - sum(ring_area(h) for h in coordinates[1:]))


def convex_hull(points: Iterable[Point]) -> list[Point]:
    pts = sorted(set(points))
    if len(pts) <= 1:
        return pts

    def cross(o: Point, a: Point, b: Point) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def ring_area(points: list[Point]) -> float:
    if len(points) < 3:
        return 0.0
    return abs(sum(
        points[i][0] * points[(i + 1) % len(points)][1]
        - points[(i + 1) % len(points)][0] * points[i][1]
        for i in range(len(points))
    )) / 2

