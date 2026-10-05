"""Административные и государственные границы (разделы 13.1 и 13.3 ТЗ)."""
from __future__ import annotations

import logging
from collections import defaultdict

import shapely
from shapely.ops import unary_union

from ..parsers.osm_geometry import relation_polygon, way_line
from ..parsers.overpass_json import uid_of
from ..recipes import FieldSpec, OutputSpec
from .common import BuildContext, Candidate

log = logging.getLogger("osmdwnl")

STATE_BORDER_FIELDS = [
    FieldSpec(name="countries"), FieldSpec(name="border_scope"), FieldSpec(name="maritime"),
    FieldSpec(name="disputed"), FieldSpec(name="parent_relations"), FieldSpec(name="member_role"),
]
GEOM_MATCH_TOLERANCE_DEG = 1e-6


def _parent_index(ctx: BuildContext, source_id: str):
    """way_id -> [(rel_id, role, iso)] по всем родительским admin_level=2 relations."""
    idx: dict[int, list[tuple[int, str, str | None]]] = defaultdict(list)
    for rid in ctx.store.border_parents.get(source_id, set()):
        rel = ctx.store.elements.get(("relation", rid)) or {}
        iso = (rel.get("tags") or {}).get("ISO3166-1")
        for m in rel.get("members") or []:
            if m.get("type") == "way":
                idx[int(m["ref"])].append((rid, m.get("role") or "", iso))
    return idx


def state_border_candidates(out: OutputSpec, ctx: BuildContext) -> list[Candidate]:
    recipe = ctx.recipe
    params = recipe.params
    mode = str(params.get("mode", "all_target_borders"))
    include_maritime = bool(params.get("include_maritime", False))
    targets = set(recipe.countries)
    if mode not in ("all_target_borders", "ru_kz_shared_only", "shared_only", "country_outlines"):
        from ..errors import RecipeError
        raise RecipeError(f"неизвестный режим state_borders: {mode}",
                          action="mode: all_target_borders | shared_only")
    shared_only = mode in ("ru_kz_shared_only", "shared_only")
    sid = out.from_[0]
    idx = _parent_index(ctx, sid)
    cands: list[Candidate] = []
    by_country: dict[str, list[Candidate]] = defaultdict(list)
    for key in ctx.store.keys_of(sid):
        if key[0] != "way":
            continue
        el = ctx.store.elements.get(key)
        if not el:
            continue
        tags = el.get("tags") or {}
        parents = idx.get(key[1], [])
        countries = sorted({iso for _, _, iso in parents if iso})
        own = targets & set(countries)
        if not own:
            continue
        maritime = tags.get("maritime") == "yes" or tags.get("boundary") == "maritime" or \
            tags.get("border_type") in ("territorial", "maritime")
        if maritime:
            scope = "maritime"
        elif len(own) >= 2:
            scope = "-".join(sorted(own, key=lambda x: recipe.countries.index(x)))
        else:
            scope = "-".join(sorted(own, key=lambda x: recipe.countries.index(x))) + "-other"
        extra = {
            "countries": ";".join(countries),
            "border_scope": scope,
            "maritime": "yes" if maritime else None,
            "disputed": tags.get("disputed"),
            "parent_relations": ";".join(f"r{r}" for r in sorted({p[0] for p in parents})),
            "member_role": ";".join(sorted({p[1] for p in parents if p[1]})) or None,
        }
        if maritime and not include_maritime:
            extra["_filtered"] = True
        c = Candidate(key, el, set(countries), geom=way_line(el), extra=extra)
        cands.append(c)
        for cc in own:
            by_country[cc].append(c)

    if shared_only:
        # общая граница — участок, разделяющий хотя бы две страны из списка
        shared = [c for c in cands if len(targets & c.countries) >= 2]
        if not shared and len(targets) == 2 and all(by_country.get(t) for t in targets):
            shared = _geometric_shared(by_country, list(targets), ctx, out.layer)
        for c in cands:
            if c not in shared:
                c.extra["_filtered"] = True
    return cands


def _geometric_shared(by_country, targets, ctx, layer) -> list[Candidate]:
    """Резерв: членство неполное — совпадение геометрий с допуском, с предупреждением."""
    a, b = targets
    lines_b = unary_union([c.geom for c in by_country[b] if c.geom is not None])
    zone = lines_b.buffer(GEOM_MATCH_TOLERANCE_DEG)
    res = []
    for c in by_country[a]:
        if c.geom is not None and zone.covers(c.geom):
            res.append(c)
            ctx.store.warn("W_SHARED_BY_GEOMETRY", layer, uid_of(c.key),
                           f"общий участок {a}-{b} определён по совпадению геометрии, а не по членству")
    return res


def country_outline_candidates(out: OutputSpec, ctx: BuildContext) -> list[Candidate]:
    cands = []
    for key in ctx.store.keys_of("__country_outlines__"):
        el = ctx.store.elements.get(key)
        if not el:
            continue
        iso = (el.get("tags") or {}).get("ISO3166-1")
        if iso not in ctx.recipe.countries:
            continue
        cands.append(Candidate(key, el, {iso}))
    return cands


def resolve_points(ctx_store, recipe, source_id: str) -> list[tuple[str, float, float]]:
    """Объекты, попавшие в несколько стран, → точки для проверки is_in."""
    pts = []
    for key, countries in ctx_store.keys_of(source_id).items():
        if len({c for c in countries if c}) <= 1:
            continue
        el = ctx_store.elements.get(key)
        if not el:
            continue
        g, _ = relation_polygon(el) if key[0] == "relation" else (None, [])
        if g is None or g.is_empty:
            continue
        p = shapely.point_on_surface(g)
        pts.append((uid_of(key), p.x, p.y))
    return pts
