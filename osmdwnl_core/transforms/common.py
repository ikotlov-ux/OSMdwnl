"""Общий конвейер построения выходных слоёв (раздел 10.2 ТЗ).

Порядок операций фиксирован: сборка → 2D → удаление пустых → make_valid → извлечение
нужного типа → (polygon_to_boundary) → дедупликация → clip → explode → multi → dissolve →
sort → rename_fields. Список operations в рецепте включает/выключает шаги, но не меняет порядок.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import geopandas as gpd
import pandas as pd
import shapely
from shapely.prepared import prep

from ..parsers.osm_geometry import (center_point, extract_type, family_of, force_2d, node_point,
                                    relation_lines, relation_polygon, to_multi, way_line, way_polygon)
from ..parsers.overpass_json import OSMStore, tags_json, uid_of
from ..recipes import FieldSpec, OutputSpec, Recipe, match_any

log = logging.getLogger("osmdwnl")

COMMON_FIELDS = ["osm_type", "osm_id", "osm_uid", "part_no", "name_ru", "name_local", "name_display",
                 "name_source", "country_code", "source_backend", "osm_version", "osm_timestamp",
                 "downloaded_at", "recipe_id"]
STAT_COLUMNS = ["downloaded", "duplicates", "filtered", "invalid", "clipped_out", "written"]


@dataclass
class Candidate:
    key: tuple[str, int]
    el: dict
    countries: set[str]
    geom: Any = None              # заранее построенная геометрия (специальные преобразования)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildContext:
    recipe: Recipe
    store: OSMStore
    aoi_geom: Any
    backend: str
    downloaded_at: datetime
    clip_override: Optional[bool] = None
    keep_tags_json: bool = True
    assigned: dict[str, set] = field(default_factory=dict)   # exclusive_group -> keys


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _real(v):
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def name_fields(tags: dict, recipe: Recipe) -> dict[str, Any]:
    pref, fb = recipe.language.preferred, recipe.language.fallback
    name_ru = next((tags[k] for k in pref if tags.get(k)), None)
    display, src = None, "missing"
    for k in pref + fb:
        if tags.get(k):
            display, src = tags[k], k
            break
    return {"name_ru": name_ru, "name_local": tags.get("name"), "name_display": display,
            "name_source": src}


def _ts(v):
    if not v:
        return pd.NaT
    try:
        return pd.Timestamp(v).tz_convert("UTC") if pd.Timestamp(v).tzinfo else pd.Timestamp(v, tz="UTC")
    except (ValueError, TypeError):
        return pd.NaT


def default_candidates(out: OutputSpec, ctx: BuildContext) -> list[Candidate]:
    merged: dict = {}
    for sid in out.from_:
        for key, countries in ctx.store.keys_of(sid).items():
            el = ctx.store.elements.get(key)
            if el is None or ("tags" not in el and "geometry" not in el and "lat" not in el
                              and "members" not in el):
                ctx.store.warn("W_MISSING_ELEMENT", out.layer, uid_of(key),
                               "объект выбран запросом, но его данные не получены")
                continue
            if key in merged:
                merged[key].countries |= countries
            else:
                merged[key] = Candidate(key, el, set(countries))
    return list(merged.values())


def raw_geometry(c: Candidate, family: str, layer: str, ctx: BuildContext):
    if c.geom is not None:
        return c.geom
    t, el = c.key[0], c.el
    if family == "point":
        p = center_point(el) if t != "node" else node_point(el)
        if p is not None:
            return p
        g = raw_geometry(Candidate(c.key, el, c.countries), "polygon" if t == "relation" else "line",
                         layer, ctx)
        return g.representative_point() if g is not None else None
    if t == "node":
        return None
    if family == "line":
        return way_line(el) if t == "way" else relation_lines(el)
    if t == "way":
        g, w = way_polygon(el)
        if w:
            ctx.store.warn(w, layer, uid_of(c.key), "незамкнутая линия не может быть полигоном")
        return g
    g, warns = relation_polygon(el)
    for w in warns:
        ctx.store.warn(w, layer, uid_of(c.key), _WARN_TEXT.get(w, w))
    return g


_WARN_TEXT = {
    "W_RING_NOT_CLOSED": "незамкнутая/неполная relation: полигон не собран и не записан",
    "W_MEMBERS_MISSING": "у части member ways нет геометрии",
    "W_RELATION_TYPE": "relation не является multipolygon/boundary",
    "W_RELATION_EMPTY": "relation не содержит пригодных колец",
    "W_RING_FALLBACK": "кольца касаются друг друга; использован резервный метод polygonize",
}


def build_layer(out: OutputSpec, ctx: BuildContext, candidates: Optional[list[Candidate]] = None,
                extra_field_specs: Optional[list[FieldSpec]] = None) -> tuple[gpd.GeoDataFrame, dict]:
    recipe, store = ctx.recipe, ctx.store
    ops = set(out.op_names())
    family = family_of(out.geometry_type)
    stats = dict.fromkeys(STAT_COLUMNS, 0)
    cands = candidates if candidates is not None else default_candidates(out, ctx)
    group = ctx.assigned.setdefault(out.exclusive_group, set()) if out.exclusive_group else None

    clip = ctx.clip_override if ctx.clip_override is not None else (
        out.clip_geometry if out.clip_geometry is not None else recipe.clip_geometry)
    if "clip" not in ops:
        clip = False
    aoi = ctx.aoi_geom
    aoi_p = prep(aoi)
    build_family = "polygon" if "polygon_to_boundary" in ops else family
    fspecs = out.field_specs() + (extra_field_specs or [])

    rows = []
    for c in cands:
        tags = c.el.get("tags") or {}
        if not match_any(tags, out.where):
            continue
        if group is not None and c.key in group:
            continue
        occ = max(1, store.occurrences.get(c.key, 1))
        stats["downloaded"] += occ
        stats["duplicates"] += occ - 1
        if recipe.require_name_ru and not any(tags.get(k) for k in recipe.language.preferred):
            stats["filtered"] += 1
            continue
        if c.extra.get("_filtered"):
            stats["filtered"] += 1
            continue
        if group is not None:
            group.add(c.key)
        g = force_2d(raw_geometry(c, build_family, out.layer, ctx))
        if g is None or g.is_empty:
            stats["invalid"] += 1
            continue
        if "make_valid" in ops and not g.is_valid:
            g = shapely.make_valid(g)
        g = extract_type(g, build_family)
        if g is None or g.is_empty:
            stats["invalid"] += 1
            store.warn("W_INVALID_GEOMETRY", out.layer, uid_of(c.key), "геометрия пуста после make_valid")
            continue
        if build_family == "polygon" and family == "line":
            g = extract_type(g.boundary, "line")
        is_clipped = False
        if clip:
            if not aoi_p.intersects(g):
                stats["clipped_out"] += 1
                continue
            if not aoi_p.covers(g):
                g = extract_type(shapely.make_valid(g.intersection(aoi)), family)
                is_clipped = True
                if g is None or g.is_empty:
                    stats["clipped_out"] += 1
                    continue
        elif not aoi_p.intersects(g):
            stats["clipped_out"] += 1
            continue
        row = _attributes(c, tags, ctx, fspecs)
        row["is_clipped"] = 1 if is_clipped else 0
        row["geometry"] = g
        rows.append(row)

    columns = COMMON_FIELDS + [f.name for f in fspecs] + ["is_clipped"] + \
        (["tags_json"] if ctx.keep_tags_json else [])
    columns = list(dict.fromkeys(columns))
    df = pd.DataFrame(rows, columns=columns + ["geometry"])

    if "dissolve" in ops and len(df):
        df = _dissolve(df, out)
    if "explode" in ops and len(df):
        df = _explode(df)
    if out.geometry_type.startswith("Multi") and len(df):
        df["geometry"] = [to_multi(g) for g in df["geometry"]]
    sort_op = next((o for o in out.operations if isinstance(o, dict) and o.get("op") == "sort"), None)
    sort_by = (sort_op or {}).get("by", ["osm_type", "osm_id", "part_no"])
    if len(df):
        df = df.sort_values(sort_by, kind="stable").reset_index(drop=True)
    gdf = gpd.GeoDataFrame(_typed(df, fspecs), geometry="geometry", crs="EPSG:4326")
    ren = next((o for o in out.operations if isinstance(o, dict) and o.get("op") == "rename_fields"), None)
    if ren:
        gdf = gdf.rename(columns=ren.get("map", {}))
    stats["written"] = len(gdf)
    return gdf, stats


def _attributes(c: Candidate, tags: dict, ctx: BuildContext, fspecs: list[FieldSpec]) -> dict:
    el, key = c.el, c.key
    row = {"osm_type": key[0], "osm_id": key[1], "osm_uid": uid_of(key), "part_no": 0}
    row.update(name_fields(tags, ctx.recipe))
    cc = sorted(x for x in c.countries if x)
    row["country_code"] = ";".join(cc) if cc else None
    row["source_backend"] = ctx.backend
    row["osm_version"] = el.get("version")
    row["osm_timestamp"] = _ts(el.get("timestamp"))
    row["downloaded_at"] = pd.Timestamp(ctx.downloaded_at)
    row["recipe_id"] = ctx.recipe.id
    for f in fspecs:
        if f.name in c.extra:
            v = c.extra[f.name]
        else:
            v = None
            for t in (f.tag or f.name).split("|"):
                v = tags.get(t)
                if v is not None:
                    break
            if f.values_ru is not None or f.default_ru is not None:
                v = (f.values_ru or {}).get(v, v) if v is not None else f.default_ru
        if f.type == "int":
            v = _int(v)
        elif f.type == "real":
            v = _real(v)
        row[f.name] = v
    if ctx.keep_tags_json:
        row["tags_json"] = tags_json(tags)
    return row


def _typed(df: pd.DataFrame, fspecs: list[FieldSpec]) -> pd.DataFrame:
    df = df.copy()
    df["osm_id"] = df["osm_id"].astype("int64")
    df["part_no"] = df["part_no"].astype("int32")
    df["is_clipped"] = df["is_clipped"].astype("int32")
    df["osm_version"] = pd.array(df["osm_version"].tolist(), dtype="Int32")
    df["osm_timestamp"] = pd.to_datetime(df["osm_timestamp"], utc=True)
    df["downloaded_at"] = pd.to_datetime(df["downloaded_at"], utc=True)
    for f in fspecs:
        if f.name not in df:
            continue
        if f.type == "int":
            df[f.name] = pd.array(df[f.name].tolist(), dtype="Int64")
        elif f.type == "real":
            df[f.name] = pd.to_numeric(df[f.name], errors="coerce").astype("float64")
        else:
            df[f.name] = df[f.name].astype("object")
    for col in ("osm_type", "osm_uid", "name_ru", "name_local", "name_display", "name_source",
                "country_code", "source_backend", "recipe_id", "tags_json"):
        if col in df:
            df[col] = df[col].astype("object")
    return df


def _explode(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rec in df.to_dict("records"):
        g = rec["geometry"]
        parts = list(g.geoms) if hasattr(g, "geoms") else [g]
        for i, p in enumerate(parts):
            r = dict(rec)
            r["geometry"] = p
            r["part_no"] = i if len(parts) > 1 else 0
            rows.append(r)
    return pd.DataFrame(rows, columns=df.columns)


def _dissolve(df: pd.DataFrame, out: OutputSpec) -> pd.DataFrame:
    op = next((o for o in out.operations if isinstance(o, dict) and o.get("op") == "dissolve"), None)
    if not op or not op.get("by"):
        raise ValueError(f"слой {out.layer}: операция dissolve требует явный ключ by")
    by = op["by"]
    out_rows = []
    for _, grp in df.groupby(by, dropna=False, sort=False):
        first = grp.iloc[0].to_dict()
        first["geometry"] = shapely.union_all(list(grp["geometry"]))
        first["osm_uid"] = ";".join(grp["osm_uid"].astype(str))[:4000]
        out_rows.append(first)
    return pd.DataFrame(out_rows, columns=df.columns)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
