"""Конвертация полигональных слоёв в линии контуров (--polygons-as-lines).

- `add` — к каждому полигональному слою добавляется линейный `<имя>_outline`
  (`lakes_polygon` → `lakes_outline`, `admin_districts` → `admin_districts_outline`);
- `replace` — полигональные слои заменяются линейными с теми же именами `<имя>_outline`.

Контур = граница полигона (внешние и внутренние кольца) как MultiLineString, атрибуты те же.
У обрезанных по AOI объектов участки контура, совпадающие с границей AOI, удаляются:
это линия обрезки, а не берег или граница объекта. Если от контура ничего не осталось
(например, море, целиком накрывающее AOI), объект в линейный слой не попадает.
"""
from __future__ import annotations

import logging

import geopandas as gpd
import shapely
from shapely.geometry import MultiLineString

log = logging.getLogger("osmdwnl")

MODES = ("no", "add", "replace")
POLY_TYPES = ("MultiPolygon", "Polygon")


def outline_name(layer: str) -> str:
    return (layer[:-len("_polygon")] if layer.endswith("_polygon") else layer) + "_outline"


def _lines(g) -> MultiLineString | None:
    if g is None or g.is_empty:
        return None
    b = shapely.line_merge(g.boundary) if g.geom_type in POLY_TYPES else g
    parts = [p for p in getattr(b, "geoms", [b]) if p.geom_type == "LineString" and not p.is_empty]
    out = []
    for p in parts:
        out.extend([q for q in getattr(p, "geoms", [p]) if q.geom_type == "LineString" and not q.is_empty])
    return MultiLineString(out) if out else None


def _strip_cut(lines, cut_zone):
    if lines is None or cut_zone is None:
        return lines
    rest = lines.difference(cut_zone)
    if rest.is_empty:
        return None
    rest = shapely.line_merge(rest) if rest.geom_type == "MultiLineString" else rest
    return _lines(rest)


def polygons_to_lines(layers: dict, stats: dict, expected: dict, clipped_layers: set, mode: str,
                      aoi_geom=None) -> list[str]:
    """Меняет layers/stats/expected/clipped_layers на месте. Возвращает имена новых слоёв."""
    if mode not in ("add", "replace"):
        return []
    cut_zone = None
    if aoi_geom is not None:
        cut_zone = aoi_geom.boundary.buffer(1e-7)   # ~1 см: граница обрезки по AOI (EPSG:4326)
    created = []
    order = list(layers)
    for name in [n for n, (_, gtype, _) in layers.items() if gtype in POLY_TYPES]:
        gdf, _, label = layers[name]
        new = outline_name(name)
        if new in layers:
            continue
        rows, geoms, dropped = [], [], 0
        clip_col = "is_clipped" in gdf.columns
        for i, g in enumerate(gdf.geometry.values):
            ln = _lines(g)
            if ln is not None and clip_col and int(gdf["is_clipped"].iloc[i] or 0) == 1:
                ln = _strip_cut(ln, cut_zone)
            if ln is None:
                dropped += 1
                continue
            rows.append(i)
            geoms.append(ln)
        out = gpd.GeoDataFrame(gdf.iloc[rows].drop(columns="geometry").reset_index(drop=True),
                               geometry=geoms, crs=gdf.crs)
        st = dict(stats.get(name, {}))
        st["clipped_out"] = st.get("clipped_out", 0) + dropped
        st["written"] = len(out)
        layers[new] = (out, "MultiLineString", f"{label} — контур (линии)")
        stats[new] = st
        expected[new] = "MultiLineString"
        if name in clipped_layers:
            clipped_layers.add(new)
        created.append(new)
        if dropped:
            log.info("%s: %d объектов без контура внутри AOI (только линия обрезки) не записаны", new, dropped)
        if mode == "replace":
            del layers[name], stats[name], expected[name]
            clipped_layers.discard(name)
    # порядок слоёв: контур сразу после исходного полигонального слоя (или на его месте)
    seq = []
    for n in order:
        if n in layers:
            seq.append(n)
        o = outline_name(n)
        if o in created:
            seq.append(o)
    for d in (layers, expected):
        items = {k: d[k] for k in seq if k in d}
        d.clear()
        d.update(items)
    return created
