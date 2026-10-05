"""Запись GeoPackage: временный файл → проверка → атомарная замена (раздел 12 ТЗ)."""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio

from .. import ATTRIBUTION, LICENSE
from ..errors import OutputError

log = logging.getLogger("osmdwnl")


def tmp_path_for(target: Path) -> Path:
    return target.with_name(target.stem + ".tmp.gpkg")


def check_target(target: Path, overwrite: bool) -> None:
    if target.exists() and not overwrite:
        raise OutputError(f"Файл уже существует: {target}", code="E_OUTPUT_EXISTS",
                          action="Добавьте --overwrite или укажите другой --output")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:  # проверка блокировки до долгой загрузки
            with open(target, "r+b"):
                pass
        except PermissionError as exc:
            raise OutputError(f"Файл открыт в другой программе: {target}", code="E_OUTPUT_LOCKED",
                              cause=str(exc), action="Закройте файл в QGIS/ArcGIS и повторите") from exc


class GeoPackageWriter:
    def __init__(self, target: Path, crs: str = "EPSG:4326", spatial_index: bool = True):
        self.target = Path(target)
        self.tmp = tmp_path_for(self.target)
        self.crs = crs
        self.spatial_index = spatial_index

    def write(self, layers: dict[str, tuple[gpd.GeoDataFrame, str, str]],
              tables: dict[str, pd.DataFrame]) -> Path:
        if "GPKG" not in pyogrio.list_drivers(write=True):
            raise OutputError("В установленном GDAL нет драйвера GPKG", code="E_NO_GPKG_DRIVER")
        if self.tmp.exists():
            self.tmp.unlink()
        try:
            for name, (gdf, gtype, label) in layers.items():
                if self.crs.upper() != "EPSG:4326":
                    gdf = gdf.to_crs(self.crs)
                desc = f"{label} | {ATTRIBUTION} | {LICENSE}"
                pyogrio.write_dataframe(
                    gdf, self.tmp, layer=name, driver="GPKG", geometry_type=gtype, promote_to_multi=False,
                    layer_options={"SPATIAL_INDEX": "YES" if self.spatial_index else "NO",
                                   "GEOMETRY_NAME": "geom", "FID": "fid", "DESCRIPTION": desc,
                                   "IDENTIFIER": name})
            for name, df in tables.items():
                pyogrio.write_dataframe(df, self.tmp, layer=name, driver="GPKG",
                                        layer_options={"FID": "fid", "DESCRIPTION": ATTRIBUTION})
        except OSError as exc:
            raise OutputError("Ошибка записи GeoPackage", cause=str(exc),
                              action="Проверьте свободное место и права на папку") from exc
        except Exception as exc:
            raise OutputError("Ошибка записи GeoPackage", cause=repr(exc)) from exc
        return self.tmp

    def commit(self) -> Path:
        try:
            os.replace(self.tmp, self.target)
        except PermissionError as exc:
            raise OutputError(f"Не удалось заменить {self.target}: файл заблокирован", code="E_OUTPUT_LOCKED",
                              cause=str(exc),
                              action=f"Закройте файл в ArcGIS/QGIS. Новый результат сохранён как {self.tmp}") from exc
        return self.target


def has_rtree(path: Path, layer: str) -> bool:
    con = sqlite3.connect(str(path))
    try:
        cur = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (f"rtree_{layer}_geom",))
        return cur.fetchone() is not None
    finally:
        con.close()
