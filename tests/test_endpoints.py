"""Зеркала Overpass: недоступное зеркало ставится на паузу; профильные фильтры без areas."""
import requests

from conftest import FakeResp
from osmdwnl_core.cache import ResponseCache
from osmdwnl_core.config import OverpassConfig
from osmdwnl_core.models import BBox
from osmdwnl_core.providers.overpass import OverpassProvider
from osmdwnl_core.querybuilder import tile_query
from osmdwnl_core.recipes import load_recipe


class Sess:
    headers = {}

    def __init__(self):
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append(url)
        if "dead" in url:
            raise requests.ConnectTimeout("connect timeout")
        return FakeResp(200, {"elements": []})

    def get(self, url, timeout=None):
        return FakeResp(200, "1 slots available now.")


def test_dead_mirror_cooldown(tmp_path):
    cfg = OverpassConfig(endpoints=["https://dead.example/api/interpreter",
                                    "https://ok.example/api/interpreter"],
                         backoff_base_seconds=0, min_pause_seconds=0, check_status=False)
    s = Sess()
    p = OverpassProvider(cfg, ResponseCache(tmp_path, enabled=False), session=s)
    p.execute("[out:json];node(1);out;", "q1")
    p._ep = 0                      # даже если вернуть текущим «мёртвое» зеркало,
    p.execute("[out:json];node(2);out;", "q2")
    assert s.calls.count("https://dead.example/api/interpreter") == 1   # повторно не вызывается


def test_profile_without_area_filter():
    r = load_recipe("admin_districts")
    b = BBox(54.6, 50.6, 55.4, 51.4)
    h = "[out:json];"
    q1 = tile_query(r, r.active_sources(), b, h, tile_countries=["RU"])
    q2 = tile_query(r, r.active_sources(), b, h, local_countries=True)
    for q in (q1, q2):
        assert "area.c_" not in q
    assert 'kind="ids", country="RU"' in q1 and 'country="KZ"' not in q1
    assert 'kind="cand", country="RU"' in q2 and 'kind="cand", country="KZ"' in q2


def _prov(tmp_path, sess, eps=("https://a.example/api/interpreter", "https://b.example/api/interpreter")):
    cfg = OverpassConfig(endpoints=list(eps), backoff_base_seconds=0, min_pause_seconds=0,
                         check_status=False)
    return OverpassProvider(cfg, ResponseCache(tmp_path, enabled=False), session=sess)


def test_read_timeout_short_cooldown(tmp_path):
    import time
    p = _prov(tmp_path, Sess())
    # requests оборачивает тайм-аут чтения тела в ConnectionError — это не «нет соединения»
    p._penalize(p.cfg.endpoints[0], requests.ConnectionError(
        "HTTPSConnectionPool(host='a', port=443): Read timed out."))
    assert p._cool[p.cfg.endpoints[0]] - time.time() < 200
    p._penalize(p.cfg.endpoints[1], requests.ConnectTimeout("connect timeout=30"))
    assert p._cool[p.cfg.endpoints[1]] - time.time() > 800


def test_short_504_is_busy_not_attempt(tmp_path):
    class S(Sess):
        n = 0

        def post(self, url, data=None, timeout=None):
            S.n += 1
            if S.n <= 7:                      # больше обычного числа попыток
                return FakeResp(504, "<html>504 Gateway</html>")
            return FakeResp(200, {"elements": []})
    p = _prov(tmp_path, S(), eps=("https://a.example/api/interpreter",))
    p.cfg.retries = 3
    assert p.execute("[out:json];node(1);out;", "q")["elements"] == []
