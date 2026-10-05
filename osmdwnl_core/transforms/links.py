"""Связь точечного и площадного слоёв одного рецепта (населённые пункты).

Точка (узел place=*) получает ``polygon_uid`` — контур, в котором она лежит; контур получает
``point_uid`` — свою точку. При нескольких кандидатах предпочтение у совпадающего названия,
затем у совпадающего place, затем у меньшего контура. ``fill_points`` дополняет точечный слой
внутренними точками контуров, у которых нет своей точки (``point_source = polygon``).
"""
from __future__ import annotations

import warnings

import geopandas as gpd
import pandas as pd

from ..recipes import Recipe, _truthy


def _same(a, b) -> bool:
    return a is not None and b is not None and not (pd.isna(a) or pd.isna(b)) and str(a) == str(b)


def apply_links(recipe: Recipe, layers: dict, stats: dict) -> None:
    for ln in recipe.links:
        if ln.points not in layers or ln.polygons not in layers:
            continue                     # один из слоёв выключен
        pts, pt_type, pt_label = layers[ln.points]
        pol, pol_type, pol_label = layers[ln.polygons]
        pts = pts.copy()
        pol = pol.copy()
        pts["point_source"] = "node"
        pts["polygon_uid"] = None
        pol["point_uid"] = None
        if len(pts) and len(pol):
            with warnings.catch_warnings():     # площадь в град² — только для сравнения контуров
                warnings.simplefilter("ignore", UserWarning)
                area = pol.geometry.area
            j = gpd.sjoin(pts[["geometry"]], pol[["geometry"]], predicate="within", how="inner")
            # j: индекс точки -> index_right (контур)
            by_point: dict = {}
            by_poly: dict = {}
            for pi, qi in zip(j.index, j["index_right"]):
                by_point.setdefault(pi, []).append(qi)
                by_poly.setdefault(qi, []).append(pi)

            def score(pi, qi):
                return (not _same(pts.at[pi, "name_display"], pol.at[qi, "name_display"]),
                        not _same(pts.at[pi, "place"], pol.at[qi, "place"]) if "place" in pts and "place" in pol
                        else True, float(area.at[qi]))
            for pi, qs in by_point.items():
                qi = min(qs, key=lambda q: score(pi, q))
                pts.at[pi, "polygon_uid"] = pol.at[qi, "osm_uid"]
            for qi, ps in by_poly.items():
                pi = min(ps, key=lambda p: score(p, qi)[:2])
                pol.at[qi, "point_uid"] = pts.at[pi, "osm_uid"]
        fill = ln.fill_points
        if isinstance(fill, str):
            fill = _truthy(recipe.params.get(fill))
        if fill and len(pol):
            lone = pol[pol["point_uid"].isna()]
            if len(lone):
                add = lone.drop(columns=["point_uid"]).copy()
                add["geometry"] = add.geometry.representative_point()
                add["point_source"] = "polygon"
                add["polygon_uid"] = add["osm_uid"]
                add = add[[c for c in pts.columns if c in add.columns]]
                pts = gpd.GeoDataFrame(pd.concat([pts, add], ignore_index=True), geometry="geometry",
                                       crs=pts.crs)
                pol.loc[lone.index, "point_uid"] = lone["osm_uid"]
        for df in (pts, pol):
            for c in ("point_source", "polygon_uid", "point_uid"):
                if c in df:
                    df[c] = df[c].astype("object")
        layers[ln.points] = (pts, pt_type, pt_label)
        layers[ln.polygons] = (pol, pol_type, pol_label)
        stats[ln.points]["written"] = len(pts)
