"""Экспорт проверенного GeoPackage в папку Shapefile (один .shp на слой).

Ограничения формата учтены явно:
- имена полей DBF — не длиннее 10 символов: сокращаются по словарю, уникальность гарантируется,
  соответствие «полное имя → имя в DBF» пишется в `_fields.csv`;
- дата-время → текст ISO 8601 (DBF хранит только дату);
- текст длиннее 254 символов обрезается (число обрезанных значений — в журнале);
- поле `tags_json` в DBF не пишется, а выносится в `<слой>_tags.csv` (osm_uid, part_no, tags_json);
- служебные таблицы (osm_download_metadata и др.) → CSV UTF-8 с BOM (открываются в Excel);
- кодировка DBF — UTF-8 (файл .cpg), CRS — файл .prj.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import shutil
from pathlib import Path

import pandas as pd
import pyogrio

from .. import ATTRIBUTION, LICENSE
from ..errors import OutputError

log = logging.getLogger("osmdwnl")

SHP_TEXT_MAX = 254
SHP_FILE_MAX = 2 * 1024 ** 3          # предел .shp/.dbf — 2 ГБ

ABBREV = {
    "name_display": "name_disp", "name_source": "name_src", "country_code": "country",
    "source_backend": "backend", "osm_version": "osm_ver", "osm_timestamp": "osm_time",
    "downloaded_at": "downloaded", "is_clipped": "is_clipped", "recipe_id": "recipe_id",
    "water_type_ru": "wtype_ru", "wetland_type_ru": "wltype_ru", "intermittent": "intermit",
    "admin_level": "admin_lvl", "border_scope": "brd_scope", "border_type": "brd_type",
    "population": "population", "place_type_ru": "ptype_ru", "railway_type_ru": "rtype_ru",
    "road_type_ru": "rdtype_ru", "electrified": "electrif", "frequency": "frequency",
    "maxspeed": "maxspeed", "surface": "surface", "operator": "operator",
}


def shp_dir_for(target: Path) -> Path:
    """Папка результата Shapefile: то же имя, что у GeoPackage, без расширения."""
    target = Path(target)
    return target.with_suffix("") if target.suffix.lower() in (".gpkg", ".shp") else target


def check_shp_target(folder: Path, overwrite: bool) -> None:
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()) and not overwrite:
        raise OutputError(f"Папка уже существует: {folder}", code="E_OUTPUT_EXISTS",
                          action="Добавьте --overwrite или укажите другой --output")
    folder.parent.mkdir(parents=True, exist_ok=True)
    if folder.exists():
        for f in folder.glob("*.dbf"):
            try:
                with open(f, "r+b"):
                    pass
            except PermissionError as exc:
                raise OutputError(f"Файл открыт в другой программе: {f}", code="E_OUTPUT_LOCKED",
                                  cause=str(exc), action="Закройте слой в QGIS/ArcGIS и повторите") from exc


def short_names(cols: list[str]) -> dict[str, str]:
    """Полное имя поля → уникальное имя DBF (≤ 10 символов)."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for c in cols:
        base = ABBREV.get(c, c)
        base = re.sub(r"[^A-Za-z0-9_]", "_", base)[:10] or "f"
        name, i = base, 1
        while name.lower() in used:
            suf = str(i)
            name = base[:10 - len(suf)] + suf
            i += 1
        used.add(name.lower())
        out[c] = name
    return out


def _csv(df: pd.DataFrame, path: Path) -> None:
    df = df.drop(columns=[c for c in ("geometry", "geom") if c in df.columns], errors="ignore")
    df.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)


def export_shapefiles(gpkg: Path, folder: Path, spatial_layers: list[str]) -> Path:
    """Пишет слои GeoPackage в `folder` (через временную папку и замену). Возвращает папку."""
    gpkg, folder = Path(gpkg), Path(folder)
    tmp = folder.with_name(folder.name + ".tmp_shp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    fields_rows = []
    truncated = 0
    try:
        all_layers = [str(r[0]) for r in pyogrio.list_layers(gpkg)]
        for name in spatial_layers:
            info = pyogrio.read_info(gpkg, layer=name)
            gdf = pyogrio.read_dataframe(gpkg, layer=name)
            if "tags_json" in gdf.columns:
                if len(gdf):
                    _csv(gdf[[c for c in ("osm_uid", "part_no", "tags_json") if c in gdf.columns]],
                         tmp / f"{name}_tags.csv")
                gdf = gdf.drop(columns=["tags_json"])
            for c in gdf.columns:
                if c == "geometry":
                    continue
                s = gdf[c]
                if pd.api.types.is_datetime64_any_dtype(s):
                    gdf[c] = s.dt.strftime("%Y-%m-%dT%H:%M:%SZ").where(s.notna(), None)
                elif s.dtype == object or pd.api.types.is_string_dtype(s):
                    lens = s.dropna().astype(str).str.len()
                    n = int((lens > SHP_TEXT_MAX).sum())
                    if n:
                        truncated += n
                        gdf[c] = s.where(s.isna(), s.astype(str).str.slice(0, SHP_TEXT_MAX))
                elif pd.api.types.is_bool_dtype(s):
                    gdf[c] = s.astype("Int32")
            ren = short_names([c for c in gdf.columns if c != "geometry"])
            for full, short in ren.items():
                fields_rows.append({"layer": name, "field_shp": short, "field_full": full})
            gdf = gdf.rename(columns=ren)
            gtype = info["geometry_type"]
            pyogrio.write_dataframe(gdf, tmp / f"{name}.shp", driver="ESRI Shapefile", encoding="UTF-8",
                                    geometry_type=gtype, promote_to_multi=False,
                                    layer_options={"SPATIAL_INDEX": "YES"})
            for ext in (".shp", ".dbf"):
                p = tmp / f"{name}{ext}"
                if p.exists() and p.stat().st_size >= SHP_FILE_MAX:
                    raise OutputError(f"Слой {name} больше 2 ГБ — предел формата Shapefile",
                                      code="E_SHP_TOO_LARGE", action="Сохраните результат в GeoPackage")
        for name in all_layers:
            if name in spatial_layers:
                continue
            _csv(pyogrio.read_dataframe(gpkg, layer=name, read_geometry=False), tmp / f"{name}.csv")
        _csv(pd.DataFrame(fields_rows, columns=["layer", "field_shp", "field_full"]), tmp / "_fields.csv")
        (tmp / "_README.txt").write_text(
            "Результат OSMdwnl в формате Shapefile: один .shp на слой (кодировка UTF-8, .cpg).\n"
            "_fields.csv — соответствие сокращённых имён полей DBF полным именам.\n"
            "<слой>_tags.csv — все теги OSM объекта (tags_json), связь по osm_uid и part_no.\n"
            "osm_download_metadata.csv, osm_layer_statistics.csv, osm_processing_warnings.csv — служебные таблицы запуска.\n"
            f"{ATTRIBUTION}; {LICENSE}\n", encoding="utf-8")
    except OutputError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OutputError("Ошибка записи Shapefile", cause=repr(exc),
                          action=f"Проверенный GeoPackage сохранён: {gpkg}") from exc
    if truncated:
        log.warning("Shapefile: %d текстовых значений длиннее %d символов обрезаны (полные — в GeoPackage "
                    "или <слой>_tags.csv)", truncated, SHP_TEXT_MAX)
    try:
        if folder.exists():
            shutil.rmtree(folder)
        os.replace(tmp, folder)
    except PermissionError as exc:
        raise OutputError(f"Не удалось заменить папку {folder}: файлы заблокированы", code="E_OUTPUT_LOCKED",
                          cause=str(exc),
                          action=f"Закройте слои в ArcGIS/QGIS. Новый результат сохранён в {tmp}") from exc
    return folder


def folder_size(folder: Path) -> int:
    return sum(p.stat().st_size for p in Path(folder).iterdir() if p.is_file())
