"""1.7.0: лёгкий заголовок, деление «застрявшего» тайла, параллельные зеркала, PBF backend."""
import json
import threading

import pytest

from conftest import FakeResp
from osmdwnl_core.cache import ResponseCache, query_hash
from osmdwnl_core.cli import main
from osmdwnl_core.config import AppConfig, OverpassConfig
from osmdwnl_core.models import BBox
from osmdwnl_core.planner import Executor
from osmdwnl_core.providers.overpass import OverpassProvider, light_header
from osmdwnl_core.recipes import load_recipe


class Sess:
    headers = {}

    def __init__(self, router):
        self.router = router
        self.posts = []
        self.lock = threading.Lock()

    def post(self, url, data=None, timeout=None):
        with self.lock:
            self.posts.append((url, data["data"]))
        return FakeResp(*self.router(url, data["data"]))

    def get(self, url, timeout=None):
        return FakeResp(200, "2 slots available now.")


def _cfg(**kw):
    c = AppConfig()
    c.overpass = OverpassConfig(endpoints=["https://a.example/api/interpreter",
                                           "https://b.example/api/interpreter"],
                                backoff_base_seconds=0, min_pause_seconds=0, check_status=False, **kw)
    c.overpass.smart_country_filter = False
    return c


def _exec(tmp_path, router, recipe="railways", **kw):
    cfg = _cfg(**kw)
    cache = ResponseCache(tmp_path / "c")
    sess = Sess(router)
    prov = OverpassProvider(cfg.overpass, cache, session=sess)
    orig_clone = prov.clone

    def clone(i):
        c = orig_clone(i)
        c.session = sess
        return c
    prov.clone = clone
    r = load_recipe(recipe)
    return Executor(cfg, prov, cache, r, prov.header()), sess, cache


def test_tiles_use_light_header(tmp_path):
    ex, sess, _ = _exec(tmp_path, lambda u, q: (200, {"elements": []}))
    ex.run([BBox(0, 0, 1, 1)])
    q = sess.posts[0][1]
    assert q.startswith(light_header(ex.provider.cfg))
    assert "[maxsize:134217728]" in q and "[timeout:120]" in q


def test_legacy_cache_reused(tmp_path):
    ex, sess, cache = _exec(tmp_path, lambda u, q: pytest.fail("должно браться из кэша"))
    from osmdwnl_core.querybuilder import tile_query
    t = BBox(0, 0, 1, 1)
    legacy = tile_query(ex.recipe, ex.recipe.active_sources(), t, ex.header)
    cache.put(query_hash(legacy, ex.provider.endpoint, ""), {"elements": []})
    ex.run([t])
    assert sess.posts == []


def test_stuck_tile_is_split(tmp_path):
    bad = "(0,0,1,1)"

    def router(url, q):
        if bad in q:
            return 504, "<html>504</html>"
        return 200, {"elements": []}
    ex, sess, _ = _exec(tmp_path, router, split_after_busy=3, concurrency=1)
    ex.provider._last_ok[0] = __import__("time").time()     # другие запросы проходят
    ex.run([BBox(0, 0, 1, 1)])
    assert sum(1 for _, q in sess.posts if bad in q) == 3
    assert sum(1 for _, q in sess.posts if bad not in q) == 4  # четыре квадранта


def test_parallel_uses_both_mirrors(tmp_path):
    import time

    def router(url, q):
        time.sleep(0.05)
        return 200, {"elements": []}
    ex, sess, _ = _exec(tmp_path, router, concurrency=2)
    ex.run([BBox(i, 0, i + 1, 1) for i in range(8)])
    urls = {u for u, _ in sess.posts}
    assert len(sess.posts) == 8 and len(urls) == 2


# ------------------------------------------------------------------ PBF
def _make_pbf(path):
    import osmium
    from osmium.osm.mutable import Node, Relation, Way
    w = osmium.SimpleWriter(str(path))
    nid = 1
    coords = {}

    def node(lon, lat, tags=None):
        nonlocal nid
        w.add_node(Node(id=nid, location=(lon, lat), tags=tags or {}, version=1))
        coords[nid] = (lon, lat)
        nid += 1
        return nid - 1
    river = [node(0.1 + 0.1 * i, 0.5) for i in range(5)]
    lake = [node(0.2, 0.2), node(0.3, 0.2), node(0.3, 0.3), node(0.2, 0.3)]
    outer = [node(0.6, 0.6), node(0.8, 0.6), node(0.8, 0.8), node(0.6, 0.8)]
    far = [node(5.0, 5.0), node(5.1, 5.0)]
    node(0.5, 0.4, {"place": "town", "name": "Тестовый"})
    w.add_way(Way(id=1, nodes=river, tags={"waterway": "river", "name": "Река"}, version=2))
    w.add_way(Way(id=2, nodes=lake + [lake[0]], tags={"natural": "water", "water": "lake"}, version=1))
    w.add_way(Way(id=3, nodes=outer + [outer[0]], tags={}, version=1))
    w.add_way(Way(id=4, nodes=far, tags={"waterway": "river"}, version=1))
    w.add_relation(Relation(id=10, members=[("w", 3, "outer")],
                            tags={"type": "multipolygon", "natural": "water", "water": "reservoir"},
                            version=1))
    w.close()


def test_pbf_backend_end_to_end(tmp_path, monkeypatch, patch_session):
    pytest.importorskip("osmium")
    import geopandas as gpd

    out = tmp_path / "out"
    folder = out / "_osm_pbf"
    folder.mkdir(parents=True)
    _make_pbf(folder / "test-latest.osm.pbf")
    import osmdwnl_core.pbf_backend as pb
    feat = {"type": "Feature", "properties": {"id": "test", "name": "Test", "iso3166-1:alpha2": ["RU"],
                                              "urls": {"pbf": "https://x.example/test-latest.osm.pbf"}},
            "geometry": {"type": "Polygon", "coordinates": [[[-1, -1], [2, -1], [2, 2], [-1, 2], [-1, -1]]]}}
    monkeypatch.setattr(pb, "load_index", lambda config, session=None: [feat])
    monkeypatch.setattr(pb, "remote_sizes", lambda ex, session=None: None)
    patch_session(lambda q: (504, "<html>504</html>"))
    batch = folder / "batch.json"
    common = ["--bbox", "0", "0", "1", "1", "--backend", "pbf", "--output", str(out), "-y",
              "--cache-dir", str(tmp_path / "c"), "--countries", "RU"]
    batch.write_text(json.dumps([common + ["--recipe", "hydro"], common + ["--recipe", "settlements"]]))
    assert main(common + ["--recipe", "hydro", "--pbf-batch", str(batch)]) == 0
    assert len(list(folder.glob("store_*.pkl"))) == 2          # отбор сразу для двух рецептов
    hydro = next(out.glob("hydro__*.gpkg"))
    rivers = gpd.read_file(hydro, layer="rivers_line")
    assert list(rivers["osm_id"]) == [1]                        # река вне AOI отброшена
    assert len(gpd.read_file(hydro, layer="lakes_polygon")) == 1
    assert len(gpd.read_file(hydro, layer="reservoirs_polygon")) == 1   # multipolygon relation
    meta = gpd.read_file(hydro, layer="osm_download_metadata")
    assert meta["backend"][0] == "pbf" and "Geofabrik" in meta["endpoint_or_source"][0]
    assert main(common + ["--recipe", "settlements", "--pbf-batch", str(batch)]) == 0
    st = next(out.glob("settlements__*.gpkg"))
    assert len(gpd.read_file(st, layer="settlements_point")) == 1
    pb.cleanup(folder)
    assert not folder.exists()


def test_pbf_select_extracts_prefers_small():
    from shapely.geometry import box
    import osmdwnl_core.pbf_backend as pb

    def f(fid, parent, b, iso=None):
        p = {"id": fid, "name": fid, "urls": {"pbf": f"https://x/{fid}.osm.pbf"}}
        if parent:
            p["parent"] = parent
        if iso:
            p["iso3166-1:alpha2"] = iso
        return {"properties": p, "geometry": box(*b).__geo_interface__}
    feats = [f("russia", None, (0, 0, 10, 10), ["RU"]), f("south", "russia", (0, 0, 5, 10)),
             f("volga", "russia", (5, 0, 10, 10)), f("kz", None, (10, 0, 20, 10), ["KZ"])]
    ex = pb.select_extracts(box(4, 1, 6, 2), feats)
    assert sorted(x.id for x in ex) == ["south", "volga"]
    assert all(x.iso == ["RU"] for x in ex)
