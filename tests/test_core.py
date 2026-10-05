"""Модульные тесты: bbox, AOI, рецепты, QL, кэш, имена, геометрия, дедупликация."""
import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from conftest import member, ring, way
from osmdwnl_core.aoi import aoi_from_bbox, read_aoi
from osmdwnl_core.cache import query_hash
from osmdwnl_core.errors import AOIError, RecipeError
from osmdwnl_core.models import BBox
from osmdwnl_core.parsers.osm_geometry import relation_polygon, way_polygon
from osmdwnl_core.parsers.overpass_json import OSMStore
from osmdwnl_core.querybuilder import tile_query
from osmdwnl_core.recipes import Recipe, clause_to_ql, list_recipes, load_recipe, match_any
from osmdwnl_core.transforms.common import name_fields


# ---------------- bbox / AOI
def test_bbox_overpass_order():
    b = BBox(50.0, 49.0, 52.0, 51.0)
    assert b.overpass() == "49,50,51,52"          # south,west,north,east
    assert b.wsen() == (50.0, 49.0, 52.0, 51.0)


def test_bbox_grid_and_quadrants():
    b = BBox(0, 0, 3, 2)
    tiles = b.grid(1.0)
    assert len(tiles) == 6
    assert abs(sum(t.area_deg2 for t in tiles) - b.area_deg2) < 1e-9
    q = b.quadrants()
    assert len(q) == 4 and q[0].e == 1.5 and q[0].n == 1


def test_bbox_invalid():
    with pytest.raises(ValueError):
        BBox(10, 5, 11, 4)
    with pytest.raises(AOIError):
        aoi_from_bbox(10, 5, 11, 4)


def test_antimeridian_bbox_split():
    a = aoi_from_bbox(170, 60, -170, 70)
    assert len(a.bboxes) == 2
    assert {(b.w, b.e) for b in a.bboxes} == {(170, 180), (-180, -170)}


def _write_aoi(tmp_path):
    poly = Polygon([(37.0, 55.0), (38.0, 55.0), (38.0, 56.0), (37.0, 56.0)])
    g = gpd.GeoDataFrame({"id": [1]}, geometry=[poly], crs=4326).to_crs(32637)
    paths = {"shp": tmp_path / "a.shp", "geojson": tmp_path / "a.geojson", "gpkg": tmp_path / "a.gpkg"}
    g.to_file(paths["shp"])
    g.to_file(paths["geojson"], driver="GeoJSON")
    g.to_file(paths["gpkg"], layer="aoi", driver="GPKG")
    return paths


def test_same_bbox_from_formats(tmp_path):
    paths = _write_aoi(tmp_path)
    boxes = [read_aoi(str(p)).bboxes[0].wsen() for p in paths.values()]
    for b in boxes[1:]:
        assert all(abs(x - y) < 1e-6 for x, y in zip(b, boxes[0]))
    w, s, e, n = boxes[0]
    assert w < 37.01 and e > 37.99 and s < 55.01 and n > 55.99


def test_multilayer_requires_layer(tmp_path):
    p = tmp_path / "m.gpkg"
    g = gpd.GeoDataFrame(geometry=[Point(1, 1).buffer(1)], crs=4326)
    g.to_file(p, layer="one")
    g.to_file(p, layer="two")
    with pytest.raises(AOIError) as ei:
        read_aoi(str(p))
    assert ei.value.code == "E_AOI_LAYER_AMBIGUOUS"
    assert read_aoi(str(p), layer="two").bboxes


def test_missing_crs(tmp_path):
    p = tmp_path / "nocrs.geojson"
    p.write_text('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
                 '"geometry":{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}}]}')
    a = read_aoi(str(p))  # GeoJSON всегда WGS84 по стандарту
    assert a.crs.startswith("EPSG:4326")
    shp = tmp_path / "n.shp"
    gpd.GeoDataFrame(geometry=[Point(0, 0).buffer(1)]).to_file(shp)
    with pytest.raises(AOIError) as ei:
        read_aoi(str(shp))
    assert ei.value.code == "E_AOI_CRS"
    assert read_aoi(str(shp), aoi_crs="EPSG:4326").bboxes


def test_points_geometry_mode_rejected(tmp_path):
    p = tmp_path / "pts.gpkg"
    gpd.GeoDataFrame(geometry=[Point(0, 0), Point(1, 1)], crs=4326).to_file(p)
    with pytest.raises(AOIError):
        read_aoi(str(p), mode="geometry")
    assert read_aoi(str(p), mode="envelope").bboxes[0].wsen() == (0, 0, 1, 1)


# ---------------- рецепты / QL
def test_builtin_recipes_load():
    ids = {r.id for r in list_recipes()}
    assert {"admin_districts", "hydro", "state_borders"} <= ids


def _recipe(**over):
    base = {"id": "t", "version": 1, "label_ru": "t",
            "sources": [{"id": "s", "filters": [{"waterway": "river"}]}],
            "outputs": [{"layer": "l", "from": ["s"], "geometry_type": "MultiLineString"}]}
    base.update(over)
    return Recipe.model_validate(base)


def test_recipe_validation_rejects_bad_key():
    with pytest.raises(Exception):
        _recipe(sources=[{"id": "s", "filters": [{'way"]; out;': "x"}]}])
    with pytest.raises(Exception):
        _recipe(sources=[{"id": "s", "filters": [{"name": 'a"b'}]}])
    with pytest.raises(Exception):
        _recipe(outputs=[{"layer": "Слой", "from": ["s"], "geometry_type": "Point"}])
    with pytest.raises(Exception):
        _recipe(outputs=[{"layer": "l", "from": ["s"], "geometry_type": "Point", "operations": ["spatial_join"]}])


def test_raw_recipe_requires_flag(tmp_path):
    p = tmp_path / "raw.yml"
    p.write_text("id: raw\nversion: 1\nlabel_ru: r\ntype: raw_overpass\nsources:\n"
                 "  - {id: q, kind: raw_overpass, query: 'node[amenity=cafe]({{bbox}}); out meta;'}\n"
                 "outputs:\n  - {layer: cafes, from: [q], geometry_type: Point}\n", encoding="utf-8")
    with pytest.raises(RecipeError):
        load_recipe(str(p))
    assert load_recipe(str(p), allow_raw=True).type == "raw_overpass"


def test_clause_ql():
    assert clause_to_ql({"waterway": "river"}) == '["waterway"="river"]'
    assert clause_to_ql({"water": ["lake", "reservoir"]}) == '["water"~"^(lake|reservoir)$"]'
    assert clause_to_ql({"water": {"exists": False}}) == '[!"water"]'
    assert clause_to_ql({"name": "*"}) == '["name"]'
    assert clause_to_ql({"ref": {"regex": "^M-[0-9]+$"}}) == '["ref"~"^M-[0-9]+$"]'
    assert clause_to_ql({"a": {"not": "b"}}) == '["a"!="b"]'


def test_match_any_python_side():
    cl = [{"natural": "water", "water": "river"}, {"waterway": "riverbank", "water": {"exists": False}}]
    assert match_any({"natural": "water", "water": "river"}, cl)
    assert match_any({"waterway": "riverbank"}, cl)
    assert not match_any({"waterway": "riverbank", "water": "lake"}, cl)


def test_profile_levels_in_query():
    r = load_recipe("admin_districts")
    r.country_profiles["KZ"]["district_admin_levels"] = [6, 5]
    q = tile_query(r, r.active_sources(), BBox(54, 50, 55, 51), "[out:json];")
    assert '["admin_level"="6"](area.c_RU)' in q
    assert '["admin_level"~"^(6|5)$"](area.c_KZ)' in q
    assert "(50,54,51,55)" in q


def test_query_hash_normalized():
    assert query_hash("node(1,2,3,4);\n  out;") == query_hash("node(1,2,3,4);   \nout;  ")
    assert query_hash("node(1,2,3,4);out;", "a") != query_hash("node(1,2,3,4);out;", "b")


# ---------------- имена
def test_name_fallback():
    r = load_recipe("hydro")
    f = name_fields({"name": "Жайық", "name:ru": "Урал"}, r)
    assert f == {"name_ru": "Урал", "name_local": "Жайық", "name_display": "Урал", "name_source": "name:ru"}
    f = name_fields({"name": "Илек"}, r)
    assert f["name_ru"] is None and f["name_display"] == "Илек" and f["name_source"] == "name"
    assert name_fields({}, r)["name_source"] == "missing"


# ---------------- геометрия
def test_multipolygon_with_inner():
    rel = {"type": "relation", "id": 1, "tags": {"type": "multipolygon"},
           "members": [member(1, ring(0, 0, 10, 10)), member(2, ring(3, 3, 6, 6), "inner")]}
    g, w = relation_polygon(rel)
    assert g is not None and abs(g.area - 91) < 1e-9 and not w


def test_ring_from_split_members():
    rel = {"type": "relation", "id": 1, "tags": {"type": "boundary"},
           "members": [member(1, [(0, 0), (10, 0), (10, 10)]), member(2, [(10, 10), (0, 10), (0, 0)])]}
    g, _ = relation_polygon(rel)
    assert abs(g.area - 100) < 1e-9


def test_unclosed_relation_not_written():
    rel = {"type": "relation", "id": 1, "tags": {"type": "multipolygon"},
           "members": [member(1, [(0, 0), (10, 0), (10, 10)])]}
    g, w = relation_polygon(rel)
    assert g is None and "W_RING_NOT_CLOSED" in w


def test_island_in_hole_even_odd():
    rel = {"type": "relation", "id": 1, "tags": {"type": "multipolygon"},
           "members": [member(1, ring(0, 0, 10, 10)), member(2, ring(2, 2, 8, 8), "inner"),
                       member(3, ring(4, 4, 6, 6), "outer")]}
    g, _ = relation_polygon(rel)
    assert abs(g.area - (100 - 36 + 4)) < 1e-9


def test_touching_rings_fallback():
    rel = {"type": "relation", "id": 1, "tags": {"type": "multipolygon"},
           "members": [member(1, ring(0, 0, 1, 1)), member(2, [(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)])]}
    g, _ = relation_polygon(rel)
    assert abs(g.area - 2) < 1e-9


def test_unclosed_way_polygon():
    g, w = way_polygon(way(1, [(0, 0), (1, 0), (1, 1)]))
    assert g is None and w == "W_UNCLOSED_WAY"


# ---------------- дедупликация тайлов
def test_store_dedup_and_version():
    st = OSMStore()
    a = way(5, [(0, 0), (1, 1)], {"waterway": "river"}, version=1)
    b = way(5, [(0, 0), (1, 1), (2, 2)], {"waterway": "river", "name": "X"}, version=2)
    mk = {"type": "osmdwnl_marker", "id": 1, "tags": {"source": "s", "kind": "data", "country": ""}}
    st.ingest({"elements": [mk, a]})
    st.ingest({"elements": [mk, b]})
    st.ingest({"elements": [mk, a]})
    assert st.occurrences[("way", 5)] == 3
    assert st.elements[("way", 5)]["version"] == 2


def test_store_country_markers():
    st = OSMStore()
    els = [{"type": "osmdwnl_marker", "id": 1, "tags": {"source": "s", "kind": "ids", "country": "RU"}},
           {"type": "way", "id": 7},
           {"type": "osmdwnl_marker", "id": 1, "tags": {"source": "s", "kind": "ids", "country": "KZ"}},
           {"type": "way", "id": 7}, {"type": "relation", "id": 9},
           {"type": "osmdwnl_marker", "id": 1, "tags": {"source": "s", "kind": "data", "country": ""}},
           way(7, [(0, 0), (1, 1)], {"waterway": "river"}),
           {"type": "osmdwnl_marker", "id": 1, "tags": {"source": "s", "kind": "rel_ids", "country": ""}},
           {"type": "relation", "id": 9}]
    st.ingest({"elements": els})
    assert st.members["s"][("way", 7)] == {"RU", "KZ"}
    assert st.missing_relations("s") == [9]
    assert ("way", 7) in st.elements and ("relation", 9) not in st.elements


def test_legacy_recipe_files_hidden(tmp_path):
    import shutil
    from osmdwnl_core.config import APP_DIR
    from osmdwnl_core.recipes import list_recipes
    shutil.copy(APP_DIR / "recipes" / "hydro.yml", tmp_path / "hydro_ru_kz.yml")
    ids = [r.id for r in list_recipes([str(tmp_path)])]
    assert ids.count("hydro") == 1 and "hydro_ru_kz" not in ids
