"""Компилятор рецептов в Overpass QL.

Каждый тайловый запрос выводит последовательность секций, разделённых служебными
элементами ``make osmdwnl_marker`` (source, country, kind). Это позволяет получить
принадлежность объектов к стране одним запросом без повторной выгрузки геометрии.
"""
from __future__ import annotations

from .models import BBox
from .recipes import Recipe, SourceSpec, clause_to_ql, ql_str

MARKER = "osmdwnl_marker"
TYPE_QL = {"node": "node", "way": "way", "relation": "rel"}


def marker(source: str, kind: str, country: str = "") -> str:
    return (f"make {MARKER} source={ql_str(source)}, kind={ql_str(kind)}, "
            f"country={ql_str(country)}; out;")


def country_area_defs(countries: list[str]) -> list[str]:
    return [f'area["ISO3166-1"={ql_str(c)}]["boundary"="administrative"]["admin_level"="2"]->.c_{c};'
            for c in countries]


def _select(src: SourceSpec, recipe: Recipe, spatial: str, country: str | None,
            b: str | None = None) -> list[str]:
    stmts = []
    for t in src.element_types():
        for clause in src.filters:
            stmts.append(f"  {TYPE_QL[t]}{clause_to_ql(clause, recipe, country)}{spatial};")
            if t == "relation" and src.include_enclosing and b:
                # relation, целиком содержащая тайл, не имеет членов в bbox — берём её через is_in
                # центра тайла (.enc), сохраняя прочие ограничения (area страны)
                stmts.append(f"  rel{clause_to_ql(clause, recipe, country)}(pivot.enc)"
                             f"{spatial.replace(b, '')};")
    return stmts


def tile_query(recipe: Recipe, sources: list[SourceSpec], bbox: BBox, header: str,
               tile_countries: list[str] | None = None, local_countries: bool = False) -> str:
    """tile_countries=None — фильтр по areas стран (медленно, для тайлов на границе).
    tile_countries=[...] — тайл целиком лежит в этих странах (границ в нём нет): выборка только
    по bbox, принадлежность к стране задаётся маркером ids; [] — тайл вне стран рецепта.
    local_countries=True — тайл пересекает границу: выборка по bbox, страна определяется локально
    по граням, на которые линии границ делят тайл (Executor.assign_local_countries)."""
    b = f"({bbox.overpass()})"
    lines = [header]
    tag_sources = [s for s in sources if s.kind == "tags"]
    scoped = [s for s in tag_sources if s.country_scope == "area"]
    if local_countries:
        tile_countries = ["__local__"]
    if scoped and tile_countries is None:
        lines += country_area_defs(recipe.countries)
    if any(x.include_enclosing for x in tag_sources):
        lines.append(f"is_in({(bbox.s + bbox.n) / 2:.7f},{(bbox.w + bbox.e) / 2:.7f})->.enc;")
    out_lines: list[str] = []
    for s in tag_sources:
        sid = s.id
        sets = []
        if s.country_scope == "area" and tile_countries is not None:
            if not tile_countries:
                continue
            if uses_profile(s):
                # фильтр зависит от страны ($profile): выборка по bbox отдельно для каждой страны
                cs = list(recipe.countries) if local_countries else tile_countries
                for c in cs:
                    setname = f"s_{_ident(sid)}_{c}"
                    lines.append("(")
                    lines += _select(s, recipe, b, c, b)
                    lines.append(f")->.{setname};")
                    sets.append(setname)
                    out_lines.append(marker(sid, "cand" if local_countries else "ids", c))
                    out_lines.append(f".{setname} out ids;")
                lines.append("(" + "".join(f".{x};" for x in sets) + f")->.s_{_ident(sid)};")
            else:
                lines.append("(")
                lines += _select(s, recipe, b, None, b)
                lines.append(f")->.s_{_ident(sid)};")
                for c in ([] if local_countries else tile_countries):
                    out_lines.append(marker(sid, "ids", c))
                    out_lines.append(f".s_{_ident(sid)} out ids;")
        elif s.country_scope == "area":
            for c in recipe.countries:
                setname = f"s_{_ident(sid)}_{c}"
                lines.append("(")
                lines += _select(s, recipe, f"(area.c_{c}){b}", c, b)
                lines.append(f")->.{setname};")
                sets.append(setname)
                out_lines.append(marker(sid, "ids", c))
                out_lines.append(f".{setname} out ids;")
            lines.append("(" + "".join(f".{x};" for x in sets) + f")->.s_{_ident(sid)};")
        else:
            lines.append("(")
            lines += _select(s, recipe, b, None, b)
            lines.append(f")->.s_{_ident(sid)};")
        allset = f"s_{_ident(sid)}"
        out_lines.append(marker(sid, "data"))
        if s.geometry_output == "center":
            out_lines.append(f".{allset} out center meta;")
        elif s.geometry_output == "tags":
            out_lines.append(f".{allset} out tags;")
        else:
            types = s.element_types()
            nw = "".join(f"{t}.{allset};" for t in ("node", "way") if t in types)
            if nw:
                out_lines.append(f"({nw}); out meta geom;")
            if "relation" in types:
                out_lines.append(marker(sid, "rel_ids"))
                out_lines.append(f"rel.{allset}; out ids;")
    for s in sources:
        if s.kind == "country_border_members":
            out_lines += border_members_block(recipe, s, b)
        if s.kind == "raw_overpass":
            body = (s.query or "").replace("{{bbox}}", bbox.overpass())
            out_lines.append(marker(s.id, "data"))
            out_lines.append(body.strip())
    return "\n".join(lines + out_lines)


def border_members_block(recipe: Recipe, src: SourceSpec, b: str) -> list[str]:
    """Member ways государственных relations, пересекающие тайл, и все их родительские relations."""
    cc = "|".join(recipe.countries)
    sid = _ident(src.id)
    return [
        f'rel["boundary"="administrative"]["admin_level"="2"]["ISO3166-1"~"^({cc})$"]->.bc_{sid};',
        f"way(r.bc_{sid}){b}->.bw_{sid};",
        f'rel(bw.bw_{sid})["boundary"="administrative"]["admin_level"="2"]->.bp_{sid};',
        marker(src.id, "parents"),
        f".bp_{sid} out body;",
        marker(src.id, "data"),
        f".bw_{sid} out meta geom;",
    ]


def border_probe_query(countries: list[str], bbox: BBox, header: str) -> str:
    """Линии государственных границ стран рецепта в пределах bbox (лёгкий запрос, out skel geom)."""
    cc = "|".join(countries)
    return "\n".join([header,
                      f'rel["boundary"="administrative"]["admin_level"="2"]["ISO3166-1"~"^({cc})$"]->.bc;',
                      f"way(r.bc)({bbox.overpass()});", "out skel geom qt;"])


def relations_query(source_id: str, rel_ids: list[int], header: str) -> str:
    ids = ",".join(str(i) for i in sorted(rel_ids))
    return "\n".join([header, marker(source_id, "data"), f"rel(id:{ids});", "out meta geom;"])


def is_in_query(points: list[tuple[str, float, float]], header: str) -> str:
    """points: (uid, lon, lat) → для каждой точки государственные areas, в которые она попадает."""
    lines = [header]
    for uid, lon, lat in points:
        lines.append(f"make {MARKER} source={ql_str(uid)}, kind={ql_str('is_in')}, country=\"\"; out;")
        lines.append(f"is_in({lat:.7f},{lon:.7f})->.a;")
        lines.append('area.a["boundary"="administrative"]["admin_level"="2"]["ISO3166-1"]; out tags;')
    return "\n".join(lines)


def country_outline_query(countries: list[str], header: str) -> str:
    cc = "|".join(countries)
    return "\n".join([header, marker("__country_outlines__", "data"),
                      f'rel["boundary"="administrative"]["admin_level"="2"]["ISO3166-1"~"^({cc})$"];',
                      "out meta geom;"])


def uses_profile(src: SourceSpec) -> bool:
    def walk(v):
        if isinstance(v, str):
            return v.startswith("$profile:")
        if isinstance(v, dict):
            return any(walk(x) for x in v.values())
        if isinstance(v, (list, tuple)):
            return any(walk(x) for x in v)
        return False
    return walk(src.filters)


def _ident(s: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in s)
