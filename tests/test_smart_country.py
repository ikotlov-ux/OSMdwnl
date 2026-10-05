"""Ускорение: страна по линиям госграниц и is_in вместо фильтра (area.c_XX)."""
import re

import pyogrio

from conftest import way
from osmdwnl_core.cli import main
from test_pipeline import MK, _args, _only_gpkg

BORDER_X = 0.5      # граница RU | KZ — вертикаль x = 0.5 внутри тайла (0..1)


def router(q):
    if "way(r.bc)" in q:                                   # линии госграниц в AOI
        return 200, {"elements": [way(900, [(BORDER_X, -1), (BORDER_X, 2)])]}
    if 'kind="is_in"' in q:                              # страна внутренних точек граней
        els = []
        for uid, lat, lon in re.findall(r'source="([^"]+)".*?\n.*?is_in\(([-\d.]+),([-\d.]+)\)', q):
            els.append(MK(uid, "is_in"))
            iso = "RU" if float(lon) < BORDER_X else "KZ"
            els.append({"type": "area", "id": 3600000000 + (1 if iso == "RU" else 2),
                        "tags": {"ISO3166-1": iso}})
        return 200, {"elements": els}
    assert "area.c_" not in q, "в ускоренном режиме фильтр по areas не нужен"
    assert '"ISO3166-1"="RU"' not in q
    ids = []
    for c in re.findall(r'source="river_lines", kind="ids", country="(\w+)"', q):   # тайл без границы
        ids += [MK("river_lines", "ids", c)] + [{"type": "way", "id": i} for i in (1, 2, 3)]
    return 200, {"elements": ids + [
        MK("river_lines", "data"),
        way(1, [(0.1, 0.2), (0.3, 0.2)], {"waterway": "river", "name": "west"}),
        way(2, [(0.4, 0.5), (0.6, 0.5)], {"waterway": "river", "name": "cross"}),
        way(3, [(0.7, 0.8), (0.9, 0.8)], {"waterway": "river", "name": "east"}),
    ]}


def test_smart_border_tile(tmp_path, patch_session):
    sess = patch_session(router, smart=True)
    assert main(_args(tmp_path, "--bbox", "0", "0", "1", "1", "--recipe", "hydro")) == 0
    df = pyogrio.read_dataframe(_only_gpkg(tmp_path), layer="rivers_line").set_index("osm_uid")
    assert df.loc["w1", "country_code"] == "RU"
    assert df.loc["w3", "country_code"] == "KZ"
    assert df.loc["w2", "country_code"] == "KZ;RU"
    assert any("way(r.bc)" in q for q in sess.posts) and any("is_in(" in q for q in sess.posts)


def test_smart_tile_without_border(tmp_path, patch_session):
    def r2(q):
        if "way(r.bc)" in q:
            return 200, {"elements": []}
        return router(q)
    sess = patch_session(r2, smart=True)
    assert main(_args(tmp_path, "--bbox", "0", "0", "0.4", "0.4", "--recipe", "hydro")) == 0
    tile_q = [q for q in sess.posts if "waterway" in q][0]
    assert 'kind="ids", country="RU"' in tile_q and 'country="KZ"' not in tile_q
    df = pyogrio.read_dataframe(_only_gpkg(tmp_path), layer="rivers_line")
    assert set(df.country_code) == {"RU"}
