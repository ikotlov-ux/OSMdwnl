import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name):
    with gzip.open(FIX / name, "rt", encoding="utf-8") as f:
        return json.load(f)


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Отвечает на POST по правилу router(query) -> (status, payload)."""

    def __init__(self, router):
        self.router = router
        self.headers = {}
        self.posts = []

    def post(self, url, data=None, timeout=None):
        q = data["data"]
        self.posts.append(q)
        st, payload = self.router(q)
        return FakeResp(st, payload)

    def get(self, url, timeout=None):
        return FakeResp(200, "2 slots available now.")


@pytest.fixture
def patch_session(monkeypatch):
    def _apply(router, smart=False):
        # фиктивные маршрутизаторы отвечают одинаково на любой запрос, поэтому по умолчанию
        # ускорение (линии границ + is_in) выключено; test_smart_* включают его явно
        if not smart:
            from osmdwnl_core.planner import Executor
            monkeypatch.setattr(Executor, "classify_tiles", lambda self, tiles, sources, **kw: None)
        import osmdwnl_core.providers.overpass as ov
        sess = FakeSession(router)
        orig = ov.OverpassProvider.__init__

        def init(self, cfg, cache, recipe_tag="", session=None):
            cfg.backoff_base_seconds = 0.0
            cfg.min_pause_seconds = 0.0
            orig(self, cfg, cache, recipe_tag, session=sess)
        monkeypatch.setattr(ov.OverpassProvider, "__init__", init)
        return sess
    return _apply


def way(wid, coords, tags=None, version=1, closed=False):
    pts = [{"lat": y, "lon": x} for x, y in coords]
    nodes = list(range(wid * 100, wid * 100 + len(coords)))
    if closed:
        nodes[-1] = nodes[0]
    el = {"type": "way", "id": wid, "version": version, "nodes": nodes, "geometry": pts}
    if tags is not None:
        el["tags"] = tags
    return el


def member(wid, coords, role="outer"):
    return {"type": "way", "ref": wid, "role": role, "geometry": [{"lat": y, "lon": x} for x, y in coords]}


def ring(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
