"""Интеграционные тесты: полный запуск CLI на фиксированных ответах Overpass (без сети)."""
import sqlite3

import pyogrio
import pytest

from conftest import load_fixture, member, ring, way
from osmdwnl_core.cli import main

MK = lambda s, k, c="": {"type": "osmdwnl_marker", "id": 1, "tags": {"source": s, "kind": k, "country": c}}  # noqa: E731


def _args(tmp_path, *extra):
    return [*extra, "--output", str(tmp_path / "out"), "--cache-dir", str(tmp_path / "cache"), "-y",
            "--log-level", "WARNING"]


def _layers(path):
    return {str(r[0]): r[1] for r in pyogrio.list_layers(path)}


def _only_gpkg(tmp_path):
    files = list((tmp_path / "out").glob("*.gpkg"))
    assert len(files) == 1, files
    return files[0]


# ------------------------------------------------ живые фикстуры (Оренбург / граница RU–KZ)
def test_state_borders_live_fixture(tmp_path, patch_session):
    data = load_fixture("live_state_borders_tile.json.gz")
    patch_session(lambda q: (200, data))
    code = main(_args(tmp_path, "--bbox", "54.6", "50.6", "55.4", "51.4", "--recipe", "state_borders"))
    assert code == 0
    f = _only_gpkg(tmp_path)
    lay = _layers(f)
    assert lay["state_border_line"] == "MultiLineString"
    assert {"osm_download_metadata", "osm_layer_statistics", "osm_processing_warnings"} <= set(lay)
    d = pyogrio.read_dataframe(f, layer="state_border_line")
    assert len(d) == 16 and set(d.border_scope) == {"RU-KZ"} and set(d.countries) == {"KZ;RU"}
    assert not d.duplicated(["osm_uid", "part_no"]).any()
    meta = pyogrio.read_dataframe(f, layer="osm_download_metadata")
    row = meta.iloc[0]
    assert row.attribution == "© OpenStreetMap contributors" and "openstreetmap.org/copyright" in row.license
    assert row.bbox_overpass_swne == "50.6,54.6,51.4,55.4"
    assert "юридическим" in row.disclaimer_ru
    con = sqlite3.connect(f)
    assert con.execute("select count(*) from sqlite_master where name='rtree_state_border_line_geom'").fetchone()[0]


def test_admin_live_fixture_two_stage(tmp_path, patch_session):
    tile = load_fixture("live_admin_tile.json.gz")
    rels = load_fixture("live_admin_relations.json.gz")
    sess = patch_session(lambda q: (200, rels if "rel(id:" in q else tile))
    code = main(_args(tmp_path, "--bbox", "54", "50.5", "56", "51.5", "--recipe", "admin_districts"))
    assert code == 0
    assert any("rel(id:" in q for q in sess.posts)
    d = pyogrio.read_dataframe(_only_gpkg(tmp_path), layer="admin_districts")
    assert len(d) == 9
    assert set(d.country_code) == {"RU", "KZ"}
    assert d.is_clipped.sum() == 0  # районы целиком по умолчанию
    orb = d[d.osm_uid == "r1398615"].iloc[0]
    assert orb.name_source == "name" and orb.name_ru is None or str(orb.name_ru) == "nan"
    # повторный запуск — только кэш, без POST
    n = len(sess.posts)
    assert main(_args(tmp_path, "--bbox", "54", "50.5", "56", "51.5", "--recipe", "admin_districts",
                      "--overwrite", "--clip")) == 0
    assert len(sess.posts) == n
    d2 = pyogrio.read_dataframe(_only_gpkg(tmp_path), layer="admin_districts")
    assert d2.is_clipped.sum() > 0


# ------------------------------------------------ синтетика: гидрография
def _hydro_response():
    els = [MK("river_lines", "ids", "RU"), {"type": "way", "id": 1},
           MK("river_lines", "ids", "KZ"), {"type": "way", "id": 1},
           MK("river_lines", "data"),
           way(1, [(0.1, 0.1), (0.9, 0.9), (1.5, 1.5)], {"waterway": "river", "name:ru": "Урал", "name": "Жайық"}),
           MK("water_polygons", "ids", "RU")] + [{"type": "way", "id": i} for i in (10, 11, 12, 13)] + [
           MK("water_polygons", "data"),
           way(10, ring(0.1, 0.1, 0.2, 0.2), {"natural": "water", "water": "river"}, closed=True),
           way(11, ring(0.3, 0.3, 0.4, 0.4), {"natural": "water", "water": "lake", "landuse": "reservoir"}, closed=True),
           way(12, ring(0.5, 0.5, 0.6, 0.6), {"landuse": "reservoir"}, closed=True),
           way(13, ring(0.7, 0.7, 0.8, 0.8), {"waterway": "riverbank"}, closed=True),
           MK("water_polygons", "rel_ids"), {"type": "relation", "id": 50},
           MK("seas", "data"),
           {"type": "node", "id": 99, "lat": 0.5, "lon": 0.55, "tags": {"place": "sea", "name": "Море"}}]
    return {"elements": els}


def _hydro_relations():
    return {"elements": [MK("water_polygons", "data"),
                         {"type": "relation", "id": 50, "version": 3,
                          "tags": {"type": "multipolygon", "natural": "water", "water": "lake", "name": "Оз"},
                          "members": [member(501, ring(0.2, 0.6, 0.45, 0.9)),
                                      member(502, ring(0.25, 0.65, 0.3, 0.7), "inner")]}]}


def test_hydro_classification_and_layers(tmp_path, patch_session):
    patch_session(lambda q: (200, _hydro_relations() if "rel(id:" in q else _hydro_response()))
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "hydro")) == 0
    f = _only_gpkg(tmp_path)
    lay = _layers(f)
    for name in ("rivers_line", "rivers_polygon", "reservoirs_polygon", "lakes_polygon", "seas_point"):
        assert name in lay
    assert "coastline_line" not in lay
    uid = lambda l: set(pyogrio.read_dataframe(f, layer=l).osm_uid)  # noqa: E731
    assert uid("rivers_polygon") == {"w10", "w13"}
    assert uid("reservoirs_polygon") == {"w12"}
    assert uid("lakes_polygon") == {"w11", "r50"}          # новый тег water=lake важнее landuse=reservoir
    rl = pyogrio.read_dataframe(f, layer="rivers_line").iloc[0]
    assert rl.country_code == "KZ;RU" and rl.name_display == "Урал" and rl.is_clipped == 1
    assert uid("seas_point") == {"n99"}
    lake = pyogrio.read_dataframe(f, layer="lakes_polygon")
    r50 = lake[lake.osm_uid == "r50"].iloc[0]
    assert len(r50.geometry.geoms[0].interiors) == 1


def test_empty_result_creates_schema(tmp_path, patch_session):
    patch_session(lambda q: (200, {"elements": []}))
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "hydro")) == 0
    f = _only_gpkg(tmp_path)
    info = pyogrio.read_info(f, layer="lakes_polygon")
    assert info["features"] == 0 and info["geometry_type"] == "MultiPolygon" and "osm_uid" in list(info["fields"])
    assert pyogrio.read_dataframe(f, layer="osm_download_metadata").iloc[0].status == "no_features"
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "hydro", "--overwrite",
                      "--fail-on-empty")) == 7


# ------------------------------------------------ синтетика: граница
def _border_response():
    ru = {"type": "relation", "id": 60189, "tags": {"ISO3166-1": "RU", "admin_level": "2", "boundary": "administrative"},
          "members": [{"type": "way", "ref": 1, "role": "outer"}, {"type": "way", "ref": 2, "role": "outer"},
                      {"type": "way", "ref": 3, "role": "outer"}]}
    kz = {"type": "relation", "id": 214665, "tags": {"ISO3166-1": "KZ", "admin_level": "2", "boundary": "administrative"},
          "members": [{"type": "way", "ref": 1, "role": "outer"}, {"type": "way", "ref": 4, "role": "outer"}]}
    cn = {"type": "relation", "id": 270056, "tags": {"ISO3166-1": "CN", "admin_level": "2", "boundary": "administrative"},
          "members": [{"type": "way", "ref": 4, "role": "outer"}]}
    return {"elements": [MK("border_members", "parents"), ru, kz, cn, MK("border_members", "data"),
                         way(1, [(0.1, 0.5), (0.9, 0.5)], {}),
                         way(2, [(0.1, 0.2), (0.9, 0.2)], {"maritime": "yes"}),
                         way(3, [(0.5, 0.6), (0.5, 0.9)]),
                         way(4, [(0.2, 0.7), (0.3, 0.8)], {"disputed": "yes"})]}


@pytest.mark.parametrize("extra,expected", [
    ([], {"w1": "RU-KZ", "w3": "RU-other", "w4": "KZ-other"}),
    (["--param", "include_maritime=true"], {"w1": "RU-KZ", "w2": "maritime", "w3": "RU-other", "w4": "KZ-other"}),
    (["--param", "mode=shared_only"], {"w1": "RU-KZ"}),
])
def test_state_border_modes(tmp_path, patch_session, extra, expected):
    patch_session(lambda q: (200, _border_response()))
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "state_borders", *extra)) == 0
    d = pyogrio.read_dataframe(_only_gpkg(tmp_path), layer="state_border_line")
    assert dict(zip(d.osm_uid, d.border_scope)) == expected
    if "w4" in expected:
        r = d[d.osm_uid == "w4"].iloc[0]
        assert r.countries == "CN;KZ" and r.disputed == "yes"


# ------------------------------------------------ планирование, ошибки, восстановление
def test_dry_run_no_network(tmp_path, patch_session):
    sess = patch_session(lambda q: pytest.fail("сеть в --dry-run"))
    assert main(_args(tmp_path, "--bbox", "50", "50", "53", "52", "--recipe", "hydro", "--dry-run")) == 0
    assert sess.posts == []


def test_timeout_splits_tile(tmp_path, patch_session):
    calls = {"n": 0}

    def router(q):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, {"elements": [], "remark": "runtime error: Query timed out in \"query\" at line 9 after 180 seconds."}
        return 200, {"elements": []}
    sess = patch_session(router)
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "state_borders")) == 0
    assert len(sess.posts) == 5   # 1 неудачный + 4 квадранта


def test_retry_on_429(tmp_path, patch_session):
    seq = [(429, "Too many"), (200, {"elements": []})]
    sess = patch_session(lambda q: seq.pop(0) if seq else (200, {"elements": []}))
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "state_borders")) == 0
    assert len(sess.posts) == 2


def test_existing_file_not_overwritten(tmp_path, patch_session):
    patch_session(lambda q: (200, {"elements": []}))
    target = tmp_path / "out" / "x.gpkg"
    target.parent.mkdir()
    target.write_bytes(b"keep")
    a = ["--bbox", "0", "0", "1", "1", "--recipe", "state_borders", "--output", str(target),
         "--cache-dir", str(tmp_path / "c"), "-y"]
    assert main(a) == 6
    assert target.read_bytes() == b"keep"


def test_bad_recipe_exit_code(tmp_path):
    p = tmp_path / "bad.yml"
    p.write_text("id: bad\nversion: 1\nlabel_ru: x\nsources: []\noutputs:\n  - {layer: l, from: [nope], geometry_type: Point}\n")
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", str(p))) == 3


def test_aoi_error_exit_code(tmp_path):
    assert main(_args(tmp_path, "--aoi", str(tmp_path / "nope.gpkg"), "--recipe", "hydro")) == 2


def test_output_crs(tmp_path, patch_session):
    patch_session(lambda q: (200, _border_response()))
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "state_borders",
                      "--output-crs", "EPSG:3857")) == 0
    assert pyogrio.read_info(_only_gpkg(tmp_path), layer="state_border_line")["crs"] == "EPSG:3857"


def test_country_outlines_optional(tmp_path, patch_session):
    outline = {"elements": [MK("__country_outlines__", "data"),
                            {"type": "relation", "id": 60189, "version": 9,
                             "tags": {"type": "boundary", "ISO3166-1": "RU", "admin_level": "2", "name:ru": "Россия"},
                             "members": [member(1, ring(-1, 0.5, 2, 2))]}]}
    patch_session(lambda q: (200, outline if "__country_outlines__" in q else _border_response()))
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "state_borders",
                      "--param", "country_outlines=true")) == 0
    d = pyogrio.read_dataframe(_only_gpkg(tmp_path), layer="country_outlines")
    assert list(d.osm_uid) == ["r60189"] and d.is_clipped.iloc[0] == 1
    assert abs(d.geometry.iloc[0].area - 0.5) < 1e-9


# ------------------------------------------------ области: регион, целиком содержащий тайл
def test_admin_regions_enclosing(tmp_path, patch_session):
    big = {"type": "relation", "id": 77, "version": 1,
           "tags": {"type": "boundary", "boundary": "administrative", "admin_level": "4",
                    "name": "Область", "name:ru": "Область", "ISO3166-2": "RU-XX"},
           "members": [member(700, ring(-5, -5, 5, 5))]}
    tile = {"elements": [MK("region_relations", "ids", "RU"), {"type": "relation", "id": 77},
                         MK("region_relations", "data"), MK("region_relations", "rel_ids"),
                         {"type": "relation", "id": 77}, MK("region_ways_tagged", "data")]}
    rels = {"elements": [big]}
    sess = patch_session(lambda q: (200, rels if "rel(id:" in q else tile))
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "admin_regions")) == 0
    tq = [q for q in sess.posts if "rel(id:" not in q][0]
    assert "is_in(0.5000000,0.5000000)->.enc;" in tq and "(pivot.enc)(area.c_RU)" in tq
    d = pyogrio.read_dataframe(_only_gpkg(tmp_path), layer="admin_regions")
    assert list(d.osm_uid) == ["r77"] and d.iloc[0].iso3166_2 == "RU-XX"


def test_countries_override_and_legacy_id(tmp_path):
    from osmdwnl_core.errors import RecipeError
    from osmdwnl_core.recipes import apply_overrides, load_recipe
    r = load_recipe("admin_districts_ru_kz")          # прежний ID
    assert r.id == "admin_districts"
    r = apply_overrides(r, {}, [], [], ["by", "MN"])  # MN — без своего профиля, берётся default
    assert r.countries == ["BY", "MN"] and r.profile_values("MN", "district_admin_levels") == ["6"]
    with pytest.raises(RecipeError):
        apply_overrides(load_recipe("hydro"), {}, [], [], ["RUS"])


# ------------------------------------------------ населённые пункты и моря
def test_settlements_links(tmp_path, patch_session):
    node = lambda i, x, y, tags: {"type": "node", "id": i, "lat": y, "lon": x, "version": 1, "tags": tags}  # noqa: E731
    tile = {"elements": [
        MK("place_nodes", "ids", "RU"), {"type": "node", "id": 1}, {"type": "node", "id": 2},
        MK("place_nodes", "data"),
        node(1, 0.15, 0.15, {"place": "village", "name": "Иваново", "population": "120"}),
        node(2, 0.8, 0.8, {"place": "hamlet", "name": "Хутор"}),
        MK("place_areas", "ids", "RU"), {"type": "way", "id": 10}, {"type": "way", "id": 11},
        MK("place_areas", "data"),
        way(10, ring(0.1, 0.1, 0.2, 0.2), {"place": "village", "name": "Иваново"}, closed=True),
        way(11, ring(0.4, 0.4, 0.5, 0.5), {"place": "village", "name": "Петрово"}, closed=True),
        MK("place_areas", "rel_ids")]}
    patch_session(lambda q: (200, tile))
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "settlements")) == 0
    f = _only_gpkg(tmp_path)
    pt = pyogrio.read_dataframe(f, layer="settlements_point").set_index("osm_uid")
    pg = pyogrio.read_dataframe(f, layer="settlements_polygon").set_index("osm_uid")
    assert pt.loc["n1", "polygon_uid"] == "w10" and pt.loc["n1", "population"] == 120
    assert pt.loc["n2", "polygon_uid"] is None or str(pt.loc["n2", "polygon_uid"]) == "nan"
    assert pt.loc["w11", "point_source"] == "polygon" and pt.loc["w11", "name_display"] == "Петрово"
    assert pg.loc["w10", "point_uid"] == "n1" and pg.loc["w11", "point_uid"] == "w11"


def test_settlements_no_fill(tmp_path, patch_session):
    tile = {"elements": [MK("place_nodes", "data"), MK("place_areas", "ids", "RU"), {"type": "way", "id": 11},
                         MK("place_areas", "data"),
                         way(11, ring(0.4, 0.4, 0.5, 0.5), {"place": "village", "name": "Петрово"}, closed=True)]}
    patch_session(lambda q: (200, tile))
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "settlements",
                      "--param", "points_from_polygons=false")) == 0
    f = _only_gpkg(tmp_path)
    assert "settlements_point" not in _layers(f) or len(pyogrio.read_dataframe(f, layer="settlements_point")) == 0


def test_caspian_goes_to_seas(tmp_path, patch_session):
    casp = {"type": "relation", "id": 3987743, "version": 1,
            "tags": {"type": "multipolygon", "natural": "water", "water": "lake", "place": "sea",
                     "name:ru": "Каспийское море", "salt": "yes"},
            "members": [member(900, ring(-3, -3, 0.5, 3))]}
    tile = {"elements": [MK("sea_polygons", "data"), MK("sea_polygons", "rel_ids"), {"type": "relation", "id": 3987743},
                         MK("water_polygons", "ids", "RU"), {"type": "relation", "id": 3987743},
                         MK("water_polygons", "data"), MK("water_polygons", "rel_ids"),
                         {"type": "relation", "id": 3987743}]}
    patch_session(lambda q: (200, {"elements": [casp]} if "rel(id:" in q else tile))
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "hydro")) == 0
    f = _only_gpkg(tmp_path)
    s = pyogrio.read_dataframe(f, layer="seas_polygon")
    assert list(s.osm_uid) == ["r3987743"] and s.iloc[0].is_clipped == 1
    if "lakes_polygon" in _layers(f):
        assert len(pyogrio.read_dataframe(f, layer="lakes_polygon")) == 0


# ------------------------------------------------ железные дороги
def test_railways_layers(tmp_path, patch_session):
    node = {"type": "node", "id": 5, "lat": 0.5, "lon": 0.5, "version": 1,
            "tags": {"railway": "station", "name": "Сортировочная", "esr:user": "620001"}}
    subway = {"type": "node", "id": 6, "lat": 0.6, "lon": 0.6, "version": 1,
              "tags": {"railway": "station", "station": "subway", "name": "Метро"}}

    def r(q):
        els = []
        for sid, w in (("rail_main", way(1, [(0.1, 0.1), (0.9, 0.9)], {"railway": "rail", "usage": "main",
                                                                       "voltage": "25000", "electrified": "contact_line"})),
                       ("rail_service", way(2, [(0.1, 0.2), (0.3, 0.2)], {"railway": "rail", "service": "siding"})),
                       ("rail_inactive", way(3, [(0.1, 0.3), (0.3, 0.3)], {"railway": "abandoned"}))):
            if f'source="{sid}"' in q:
                els += [MK(sid, "ids", "RU"), {"type": "way", "id": w["id"]}, MK(sid, "data"), w]
        if 'source="rail_stations"' in q:
            els += [MK("rail_stations", "ids", "RU"), {"type": "node", "id": 5}, {"type": "node", "id": 6},
                    MK("rail_stations", "data"), node, subway]
        return 200, {"elements": els}
    sess = patch_session(r)
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "railways",
                      "--param", "inactive=true")) == 0
    assert not any("rail_urban" in q or 'source="rail_service"' in q for q in sess.posts)   # выключенные слои не грузятся
    f = _only_gpkg(tmp_path)
    lyr = _layers(f)
    assert "railway_service_line" not in lyr and "urban_rail_line" not in lyr
    main_l = pyogrio.read_dataframe(f, layer="railways_line")
    assert list(main_l.osm_uid) == ["w1"] and main_l.iloc[0].voltage == 25000
    assert list(pyogrio.read_dataframe(f, layer="railway_inactive_line").osm_uid) == ["w3"]
    st = pyogrio.read_dataframe(f, layer="railway_stations_point")
    assert list(st.osm_uid) == ["n5"] and st.iloc[0].esr_code == "620001"


# ------------------------------------------------ автомобильные дороги
def test_roads_layers(tmp_path, patch_session):
    def r(q):
        els = []
        for sid, w in (("roads_main", way(1, [(0.1, 0.1), (0.9, 0.9)], {"highway": "primary", "ref": "Р-229",
                                                                        "lanes": "4", "surface": "asphalt"})),
                       ("roads_track", way(2, [(0.1, 0.2), (0.3, 0.2)], {"highway": "track", "tracktype": "grade3"})),
                       ("roads_local", way(3, [(0.1, 0.3), (0.3, 0.3)], {"highway": "residential"}))):
            if f'source="{sid}"' in q:
                els += [MK(sid, "ids", "RU"), {"type": "way", "id": w["id"]}, MK(sid, "data"), w]
        return 200, {"elements": els}
    sess = patch_session(r)
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "roads", "--param", "tracks=true")) == 0
    assert not any('source="roads_local"' in q or 'source="roads_service"' in q for q in sess.posts)
    f = _only_gpkg(tmp_path)
    m = pyogrio.read_dataframe(f, layer="roads_main_line")
    assert list(m.osm_uid) == ["w1"] and m.iloc[0].ref == "Р-229" and m.iloc[0].lanes == 4
    t = pyogrio.read_dataframe(f, layer="roads_track_line")
    assert list(t.osm_uid) == ["w2"] and t.iloc[0].tracktype == "grade3"
    assert "roads_local_line" not in _layers(f)
