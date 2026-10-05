"""Построение геометрий из Overpass JSON: точки, линии, полигоны, multipolygon/boundary relations."""
from __future__ import annotations

from array import array
from collections import Counter
from functools import reduce
from typing import Optional

import shapely
from shapely.geometry import (GeometryCollection, LineString, MultiLineString, MultiPoint,
                              MultiPolygon, Point, Polygon)
from shapely.ops import linemerge, polygonize, unary_union

POLY_RELATION_TYPES = {"multipolygon", "boundary"}


def _coords(geometry) -> list[tuple[float, float]]:
    """Список (lon, lat): из Overpass-геометрии [{lat, lon}…] или из компактного массива
    array('d', [lon, lat, lon, lat…]), который строит PBF backend."""
    out = []
    if isinstance(geometry, array):
        for xy in zip(geometry[0::2], geometry[1::2]):
            if not out or out[-1] != xy:
                out.append(xy)
        return out
    for p in geometry or []:
        if p is None:
            continue
        xy = (float(p["lon"]), float(p["lat"]))
        if not out or out[-1] != xy:
            out.append(xy)
    return out


coords_of = _coords


def node_point(el: dict) -> Optional[Point]:
    if "lat" in el and "lon" in el:
        return Point(float(el["lon"]), float(el["lat"]))
    return None


def center_point(el: dict) -> Optional[Point]:
    if "center" in el:
        return Point(float(el["center"]["lon"]), float(el["center"]["lat"]))
    return node_point(el)


def way_line(el: dict) -> Optional[LineString]:
    c = _coords(el.get("geometry"))
    return LineString(c) if len(c) >= 2 else None


def way_is_closed(el: dict) -> bool:
    nodes = el.get("nodes")
    if nodes and len(nodes) >= 4:
        return nodes[0] == nodes[-1]
    c = _coords(el.get("geometry"))
    return len(c) >= 4 and c[0] == c[-1]


def way_polygon(el: dict) -> tuple[Optional[Polygon], Optional[str]]:
    c = _coords(el.get("geometry"))
    if len(c) < 4 or not way_is_closed(el):
        return None, "W_UNCLOSED_WAY"
    if c[0] != c[-1]:
        c.append(c[0])
    return Polygon(c), None


def relation_member_lines(el: dict, roles: Optional[set[str]] = None) -> list[LineString]:
    lines = []
    for m in el.get("members") or []:
        if m.get("type") != "way":
            continue
        if roles is not None and (m.get("role") or "") not in roles:
            continue
        c = _coords(m.get("geometry"))
        if len(c) >= 2:
            lines.append(LineString(c))
    return lines


def _missing_members(el: dict) -> int:
    return sum(1 for m in el.get("members") or []
               if m.get("type") == "way" and (m.get("role") or "") in ("outer", "inner", "")
               and not m.get("geometry"))


def _odd_endpoints(lines: list[LineString]) -> int:
    cnt: Counter = Counter()
    for ln in lines:
        cs = list(ln.coords)
        if cs[0] == cs[-1]:
            continue
        cnt[_r(cs[0])] += 1
        cnt[_r(cs[-1])] += 1
    return sum(1 for v in cnt.values() if v % 2)


def _r(xy):
    return (round(xy[0], 7), round(xy[1], 7))


def relation_polygon(el: dict) -> tuple[Optional[MultiPolygon], list[str]]:
    """Сборка multipolygon/boundary relation. Возвращает (геометрия|None, коды предупреждений).

    Основной метод — сшивание колец (linemerge) и правило even-odd по вложенности колец,
    что устойчиво к ошибкам ролей outer/inner. Резервный метод — polygonize по ролям.
    """
    warns: list[str] = []
    rtype = (el.get("tags") or {}).get("type")
    if rtype not in POLY_RELATION_TYPES:
        return None, ["W_RELATION_TYPE"]
    if _missing_members(el):
        warns.append("W_MEMBERS_MISSING")
    lines = relation_member_lines(el, {"outer", "inner", ""})
    if not lines:
        return None, warns + ["W_RELATION_EMPTY"]
    if _odd_endpoints(lines):
        return None, warns + ["W_RING_NOT_CLOSED"]
    merged = linemerge(lines)
    parts = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    rings = [p for p in parts if p.is_closed and len(p.coords) >= 4]
    if len(rings) == len(parts):
        polys = []
        for r in rings:
            pg = shapely.make_valid(Polygon(r.coords))
            polys.append(_polygonal(pg))
        polys = [p for p in polys if p is not None and not p.is_empty]
        if not polys:
            return None, warns + ["W_RELATION_EMPTY"]
        geom = reduce(lambda a, b: a.symmetric_difference(b), polys)
    else:  # касающиеся кольца: linemerge не может их разделить
        warns.append("W_RING_FALLBACK")
        outer = list(polygonize(unary_union(relation_member_lines(el, {"outer", ""}))))
        inner = list(polygonize(unary_union(relation_member_lines(el, {"inner"}))))
        if not outer:
            return None, warns + ["W_RING_NOT_CLOSED"]
        geom = unary_union(outer)
        if inner:
            geom = geom.difference(unary_union(inner))
    geom = _polygonal(shapely.make_valid(geom))
    if geom is None or geom.is_empty:
        return None, warns + ["W_RELATION_EMPTY"]
    return to_multi(geom), warns


def relation_lines(el: dict) -> Optional[MultiLineString]:
    lines = relation_member_lines(el)
    if not lines:
        return None
    return to_multi(linemerge(lines))


def _polygonal(g):
    if g is None or g.is_empty:
        return None
    if isinstance(g, (Polygon, MultiPolygon)):
        return g
    if isinstance(g, GeometryCollection):
        polys = [x for x in g.geoms if isinstance(x, (Polygon, MultiPolygon))]
        return unary_union(polys) if polys else None
    return None


def extract_type(g, family: str):
    """Извлечь из (возможной) GeometryCollection только нужное семейство: point|line|polygon."""
    if g is None or g.is_empty:
        return None
    cls = {"point": (Point, MultiPoint), "line": (LineString, MultiLineString),
           "polygon": (Polygon, MultiPolygon)}[family]
    if isinstance(g, cls):
        return g
    if hasattr(g, "geoms"):
        parts = []
        for x in g.geoms:
            e = extract_type(x, family)
            if e is not None:
                parts.extend(list(e.geoms) if hasattr(e, "geoms") else [e])
        if not parts:
            return None
        return to_multi(unary_union(parts) if family == "polygon" else
                        (MultiLineString(parts) if family == "line" else MultiPoint(parts)))
    return None


def to_multi(g):
    if g is None:
        return None
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    if isinstance(g, LineString):
        return MultiLineString([g])
    if isinstance(g, Point):
        return MultiPoint([g])
    return g


def family_of(geometry_type: str) -> str:
    return {"Point": "point", "MultiPoint": "point", "LineString": "line",
            "MultiLineString": "line", "Polygon": "polygon", "MultiPolygon": "polygon"}[geometry_type]


def force_2d(g):
    return shapely.force_2d(g) if g is not None else None
