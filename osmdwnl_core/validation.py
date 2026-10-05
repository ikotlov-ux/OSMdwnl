"""Контроль качества результата перед атомарной заменой (раздел 15 ТЗ)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pyogrio
import shapely
from shapely.prepared import prep

from .writers.gpkg import has_rtree

META_TABLES = ("osm_download_metadata", "osm_layer_statistics", "osm_processing_warnings")


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    checks: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_gpkg(path: Path, expected: dict[str, str], stats: dict[str, dict], aoi_geom=None,
                  clipped_layers: set[str] | None = None, crs: str = "EPSG:4326",
                  spatial_index: bool = True) -> ValidationReport:
    rep = ValidationReport()

    def check(cond, msg):
        rep.checks += 1
        if not cond:
            rep.errors.append(msg)

    try:
        layers = {str(r[0]): r[1] for r in pyogrio.list_layers(path)}
    except Exception as exc:
        rep.errors.append(f"GDAL не открывает файл: {exc}")
        return rep
    for t in META_TABLES:
        check(t in layers, f"нет служебной таблицы {t}")
    aoi_p = prep(aoi_geom.buffer(1e-9)) if aoi_geom is not None else None
    for name, gtype in expected.items():
        if name not in layers:
            rep.errors.append(f"нет слоя {name}")
            continue
        info = pyogrio.read_info(path, layer=name)
        check(info["geometry_type"] == gtype, f"{name}: тип {info['geometry_type']} вместо {gtype}")
        check(info["crs"] is not None and info["crs"].upper() == crs.upper(),
              f"{name}: CRS {info['crs']} вместо {crs}")
        if spatial_index:
            check(has_rtree(path, name), f"{name}: нет пространственного индекса RTree")
        n = info["features"]
        check(n == stats.get(name, {}).get("written", n),
              f"{name}: записано {n}, ожидалось {stats.get(name, {}).get('written')}")
        if n == 0:
            continue
        df = pyogrio.read_dataframe(path, layer=name, columns=["osm_uid", "part_no"])
        geoms = df.geometry.values
        check(not any(g is None or g.is_empty for g in geoms), f"{name}: есть пустые геометрии")
        check(bool(shapely.is_valid(geoms).all()), f"{name}: есть невалидные геометрии")
        check(not df.duplicated(["osm_uid", "part_no"]).any(), f"{name}: osm_uid+part_no не уникален")
        if aoi_p is not None and clipped_layers and name in clipped_layers and crs.upper() == "EPSG:4326":
            check(all(aoi_p.intersects(g) for g in geoms), f"{name}: объекты вне AOI")
    return rep
