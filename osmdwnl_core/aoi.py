"""Чтение AOI из векторных форматов или bbox (раздел 5 ТЗ)."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from pyproj import CRS
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union

from .errors import AOIError
from .models import AOIModel, BBox

log = logging.getLogger("osmdwnl")


def _safe_name(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" ._")
    return text or "aoi"


def list_vector_layers(path: str) -> list[str]:
    import pyogrio
    try:
        return [str(x[0]) for x in pyogrio.list_layers(path)]
    except Exception as exc:  # повреждённый/неподдерживаемый файл
        raise AOIError(f"Не удалось открыть AOI: {path}", cause=str(exc),
                       action="Проверьте путь и формат файла (GPKG, SHP, GeoJSON, KML, GDB, FGB)") from exc


def _split_antimeridian(geom) -> tuple[list[BBox], object]:
    """Если геометрия пересекает антимеридиан (в сдвинутой системе 0..360), делит её."""
    minx, miny, maxx, maxy = geom.bounds
    if maxx <= 180:
        return [BBox(minx, miny, maxx, maxy)], geom
    west = geom.intersection(box(minx, miny, 180, maxy))
    east = affinity.translate(geom.intersection(box(180, miny, maxx, maxy)), xoff=-360)
    boxes = []
    if not west.is_empty:
        b = west.bounds
        boxes.append(BBox(b[0], b[1], 180.0, b[3]))
    if not east.is_empty:
        b = east.bounds
        boxes.append(BBox(-180.0, b[1], b[2], b[3]))
    return boxes, unary_union([g for g in (west, east) if not g.is_empty])


def _maybe_unwrap(geom):
    """Эвристика антимеридиана: объекты по обе стороны ±180 с суммарной шириной > 180°."""
    minx, _, maxx, _ = geom.bounds
    if maxx - minx <= 180:
        return geom
    parts = list(getattr(geom, "geoms", [geom]))
    shifted = [affinity.translate(p, xoff=360) if p.bounds[2] < 0 else p for p in parts]
    cand = unary_union(shifted)
    if cand.bounds[2] - cand.bounds[0] < maxx - minx:
        log.warning("AOI пересекает антимеридиан: запрос будет разбит на два bbox")
        return cand
    return geom


def aoi_from_bbox(w: float, s: float, e: float, n: float) -> AOIModel:
    try:
        if w > e:  # пересечение антимеридиана
            boxes = [BBox(w, s, 180.0, n), BBox(-180.0, s, e, n)]
        else:
            boxes = [BBox(w, s, e, n)]
    except ValueError as exc:
        raise AOIError("Неверный bbox", cause=str(exc),
                       action="Порядок --bbox: west south east north, градусы EPSG:4326") from exc
    geom = unary_union([b.polygon() for b in boxes])
    return AOIModel(source="bbox", layer=None, crs="EPSG:4326", geometry=geom, bboxes=boxes,
                    mode="envelope", name="bbox_" + boxes[0].label() if len(boxes) == 1 else
                    f"bbox_{w}_{s}_{e}_{n}".replace(".", "p"))


def read_aoi(path: str, layer: Optional[str] = None, aoi_crs: Optional[str] = None,
             mode: str = "envelope", interactive_chooser=None) -> AOIModel:
    import geopandas as gpd

    p = Path(path)
    if not p.exists():
        raise AOIError(f"Файл AOI не найден: {path}")
    layers = list_vector_layers(str(p))
    if not layers:
        raise AOIError(f"В источнике нет векторных слоёв: {path}")
    if layer is None:
        if len(layers) == 1:
            layer = layers[0]
        elif interactive_chooser is not None:
            layer = interactive_chooser(layers)
        else:
            raise AOIError(f"Источник содержит несколько слоёв: {', '.join(layers)}",
                           code="E_AOI_LAYER_AMBIGUOUS", action="Укажите слой параметром --aoi-layer")
    elif layer not in layers:
        raise AOIError(f"Слой '{layer}' не найден. Доступны: {', '.join(layers)}")

    try:
        gdf = gpd.read_file(str(p), layer=layer, engine="pyogrio")
    except Exception as exc:
        raise AOIError(f"Не удалось прочитать слой '{layer}'", cause=str(exc)) from exc

    warnings: list[str] = []
    if gdf.crs is None:
        if not aoi_crs:
            raise AOIError("У AOI не задана система координат", code="E_AOI_CRS",
                           action="Передайте --aoi-crs, например --aoi-crs EPSG:32637")
        gdf = gdf.set_crs(aoi_crs)
    elif aoi_crs:
        warnings.append(f"--aoi-crs {aoi_crs} проигнорирован: CRS задана в файле ({gdf.crs.to_string()})")
    src_crs = CRS.from_user_input(gdf.crs).to_string()

    empty = gdf.geometry.isna() | gdf.geometry.is_empty
    if empty.any():
        warnings.append(f"Пропущено пустых геометрий AOI: {int(empty.sum())}")
        gdf = gdf[~empty]
    if gdf.empty:
        raise AOIError("После удаления пустых геометрий AOI не содержит объектов")

    gtypes = set(gdf.geom_type.unique())
    polygonal = gtypes <= {"Polygon", "MultiPolygon"}
    if not polygonal and mode == "geometry":
        raise AOIError(f"Режим geometry допустим только для полигонов (в слое: {', '.join(sorted(gtypes))})",
                       action="Используйте --aoi-mode envelope")

    gdf = gdf.to_crs(4326)
    union = unary_union(list(gdf.geometry.make_valid() if polygonal else gdf.geometry))
    union = _maybe_unwrap(union)
    minx, miny, maxx, maxy = union.bounds
    if maxx - minx <= 0 or maxy - miny <= 0:
        raise AOIError("Экстент AOI имеет нулевую площадь", action="Используйте полигональный AOI или --bbox")
    if miny < -90 or maxy > 90 or minx < -180 or maxx > 540:
        raise AOIError(f"Координаты AOI вне допустимого диапазона: {union.bounds}")

    clip_geom = box(minx, miny, maxx, maxy) if mode == "envelope" else union
    bboxes, clip_geom = _split_antimeridian(clip_geom)
    if isinstance(clip_geom, Polygon):
        clip_geom = MultiPolygon([clip_geom])
    name = _safe_name(p.stem if p.suffix.lower() != ".gdb" else p.name[:-4])
    if len(layers) > 1:
        name = f"{name}_{_safe_name(layer)}"
    for w in warnings:
        log.warning(w)
    return AOIModel(source=str(p), layer=layer, crs=src_crs, geometry=clip_geom, bboxes=bboxes,
                    mode=mode, name=name, warnings=warnings)
