"""PBF backend: выгрузки Geofabrik → локальный отбор объектов рецептов (pyosmium).

Схема работы
------------
1. По индексу Geofabrik (index-v1.json) выбираются самые мелкие выгрузки (федеральные округа,
   страны), покрывающие AOI; файлы *.osm.pbf скачиваются в папку результата (подпапка _osm_pbf).
2. Каждая выгрузка читается двумя проходами: relations (теги рецептов → список member ways) и
   nodes+ways с координатами узлов (индекс координат — внутри libosmium). Отбор по тегам и bbox AOI
   выполняется сразу для всех рецептов пакета; результат каждого рецепта сохраняется в
   _osm_pbf/store_*.pkl и берётся оттуда следующими заданиями пакета.
3. Страна объектов — по граням, на которые линии госграниц делят тайлы (несколько лёгких запросов
   Overpass: линии границ + is_in). Если Overpass недоступен — по стране выгрузки Geofabrik.
4. Relations, у которых часть членов лежит за пределами скачанных выгрузок (например, море),
   догружаются целиком по ID через Overpass.
5. После успешной обработки скачанные файлы и промежуточные данные удаляются.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import time
from array import array
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from .errors import ArgumentError, NetworkError
from .models import AOIModel, BBox
from .parsers.overpass_json import OSMStore
from .planner import Executor
from .recipes import Recipe, SourceSpec, _resolve, match_any, recipe_fingerprint

log = logging.getLogger("osmdwnl")

PBF_DIR_NAME = "_osm_pbf"
INDEX_TTL_DAYS = 7


# =========================================================================== Geofabrik
class Extract:
    def __init__(self, fid: str, name: str, url: str, iso: list[str], geom, size: Optional[int] = None):
        self.id, self.name, self.url, self.iso, self.geom, self.size = fid, name, url, iso, geom, size

    @property
    def filename(self) -> str:
        return f"{self.id}-latest.osm.pbf"

    def __repr__(self) -> str:  # pragma: no cover
        return f"Extract({self.id}, {self.size})"


def require_osmium() -> None:
    try:
        import osmium  # noqa: F401
        import osmium.filter  # noqa: F401
        if not hasattr(osmium, "FileProcessor"):
            raise ImportError("pyosmium < 4.0")
    except ImportError as exc:
        raise ArgumentError("Для PBF нужен пакет osmium (pyosmium 4.0 или новее)", cause=str(exc),
                            action="Установите: pip install osmium  (или conda install -c conda-forge pyosmium)") from exc


def _session():
    import requests
    from . import SOFTWARE_NAME, __version__
    s = requests.Session()
    s.headers["User-Agent"] = f"{SOFTWARE_NAME}/{__version__}"
    return s


def load_index(config, session=None) -> list[dict]:
    """Индекс выгрузок Geofabrik; хранится в папке данных программы, обновляется раз в неделю."""
    from .config import default_data_dir
    path = default_data_dir() / "geofabrik_index-v1.json"
    fresh = path.exists() and time.time() - path.stat().st_mtime < INDEX_TTL_DAYS * 86400
    if not fresh:
        try:
            r = (session or _session()).get(config.pbf.index_url, timeout=(20, 120))
            r.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(r.content)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            if not path.exists():
                raise NetworkError("Индекс выгрузок Geofabrik не получен", cause=str(exc),
                                   action="Проверьте доступ к download.geofabrik.de или используйте Overpass") from exc
            log.warning("Индекс Geofabrik не обновлён (%s) — используется сохранённый", exc)
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def select_extracts(aoi_geom, features: list[dict]) -> list[Extract]:
    """Минимальный набор «листовых» выгрузок (без дочерних), покрывающий AOI.

    Жадно: сначала самые мелкие выгрузки; выгрузка берётся, если добавляет ≥ 0,5 % площади AOI."""
    from shapely.geometry import shape
    from shapely.ops import unary_union
    by_id = {f["properties"]["id"]: f for f in features}
    parents = {f["properties"].get("parent") for f in features}

    def iso_of(fid: str) -> list[str]:
        seen = set()
        while fid and fid not in seen:
            seen.add(fid)
            p = by_id.get(fid, {}).get("properties", {})
            if p.get("iso3166-1:alpha2"):
                return list(p["iso3166-1:alpha2"])
            fid = p.get("parent")
        return []

    cands = []
    for f in features:
        p = f["properties"]
        if p["id"] in parents or not (p.get("urls") or {}).get("pbf") or not f.get("geometry"):
            continue
        g = shape(f["geometry"])
        if not g.intersects(aoi_geom):
            continue
        cands.append((g.area, p, g))
    cands.sort(key=lambda x: x[0])
    total = aoi_geom.area or 1e-12
    chosen: list[Extract] = []
    covered = None
    for _, p, g in cands:
        part = g.intersection(aoi_geom)
        add = part if covered is None else part.difference(covered)
        if add.area < 0.005 * total and covered is not None:
            continue
        chosen.append(Extract(p["id"], p.get("name", p["id"]), p["urls"]["pbf"], iso_of(p["id"]), g))
        covered = part if covered is None else unary_union([covered, part])
    if not chosen:
        raise ArgumentError("Для AOI нет выгрузок Geofabrik", action="Проверьте CRS AOI или используйте Overpass")
    rest = aoi_geom.difference(covered).area if covered is not None else total
    if rest > 0.10 * total:      # небольшие пропуски — обычно открытое море вне выгрузок
        log.warning("Выгрузки Geofabrik покрывают не весь AOI (не покрыто %.0f %%)", 100 * rest / total)
    return chosen


def remote_sizes(extracts: list[Extract], session=None) -> None:
    s = session or _session()
    for x in extracts:
        try:
            r = s.head(x.url, allow_redirects=True, timeout=(15, 30))
            x.size = int(r.headers.get("Content-Length")) if r.ok and r.headers.get("Content-Length") else None
        except Exception:  # noqa: BLE001 — размер только для оценки
            x.size = None


def estimate(aoi: AOIModel, config, session=None) -> dict[str, Any]:
    """Оценка для окна выбора источника: выгрузки, объём, ориентировочное время."""
    feats = load_index(config, session)
    ex = select_extracts(_aoi_shape(aoi), feats)
    remote_sizes(ex, session)
    total = sum(x.size or 0 for x in ex)
    mb = total / 1048576
    minutes = (mb / max(0.1, config.pbf.download_speed_mb_s) + mb / max(0.1, config.pbf.process_speed_mb_s)) / 60
    return {"extracts": ex, "bytes": total, "unknown": any(x.size is None for x in ex), "minutes": minutes}


def _aoi_shape(aoi: AOIModel):
    if aoi.mode == "geometry":
        return aoi.geometry
    from shapely.geometry import box
    w, s, e, n = aoi.envelope_wsen
    return box(w, s, e, n)


def pbf_dir_for(target: Path) -> Path:
    return Path(target).parent / PBF_DIR_NAME


def download(ex: Extract, folder: Path, session=None) -> Path:
    """Скачивает выгрузку (с докачкой .part); готовый файл того же размера повторно не скачивается."""
    s = session or _session()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / ex.filename
    if ex.size is None:
        remote_sizes([ex], s)
    if dest.exists() and (ex.size is None or dest.stat().st_size == ex.size):
        log.info("PBF %s: уже скачан (%.0f МБ)", ex.id, dest.stat().st_size / 1048576)
        return dest
    part = dest.with_name(dest.name + ".part")
    done = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={done}-"} if done else {}
    t0, last = time.time(), 0.0
    for attempt in range(1, 6):
        try:
            with s.get(ex.url, stream=True, headers=headers, timeout=(20, 120), allow_redirects=True) as r:
                if r.status_code == 200 and done:
                    done = 0                 # сервер не поддержал докачку — заново
                elif r.status_code not in (200, 206):
                    raise NetworkError(f"Geofabrik: HTTP {r.status_code} для {ex.url}")
                with open(part, "ab" if done else "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last > 15:
                            last = now
                            pct = f" ({100 * done / ex.size:.0f} %)" if ex.size else ""
                            log.info("PBF %s: %.0f МБ%s, %.1f МБ/с", ex.id, done / 1048576, pct,
                                     done / 1048576 / max(0.1, now - t0))
            break
        except Exception as exc:  # noqa: BLE001
            if attempt >= 5:
                raise NetworkError(f"Не удалось скачать {ex.url}", cause=str(exc),
                                   action="Повторите запуск: скачанная часть сохранится и будет докачана") from exc
            log.warning("PBF %s: обрыв загрузки (%s) — повтор %d/5", ex.id, exc, attempt)
            time.sleep(5 * attempt)
            done = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={done}-"} if done else {}
    if ex.size and part.stat().st_size != ex.size:
        raise NetworkError(f"PBF {ex.id}: размер {part.stat().st_size} вместо {ex.size}",
                           action="Повторите запуск — файл будет докачан")
    os.replace(part, dest)
    log.info("PBF %s: скачан, %.0f МБ за %.0f с", ex.id, dest.stat().st_size / 1048576, time.time() - t0)
    return dest


def cleanup(folder: Path, keep_pbf: bool = False) -> None:
    """Удаляет промежуточные данные (и скачанные PBF, если keep_pbf=False) и пустую папку."""
    folder = Path(folder)
    if not folder.is_dir():
        return
    n, size = 0, 0
    for p in folder.iterdir():
        is_pbf = p.name.endswith(".osm.pbf") or p.name.endswith(".osm.pbf.part")
        if p.is_file() and (not is_pbf or not keep_pbf) and (
                is_pbf or p.suffix in (".pkl", ".json", ".tmp")):
            size += p.stat().st_size
            try:
                p.unlink()
                n += 1
            except OSError as exc:
                log.warning("Не удалось удалить %s: %s", p, exc)
    try:
        folder.rmdir()
    except OSError:
        pass
    if n:
        log.info("PBF: удалено файлов %d (%.0f МБ) из %s", n, size / 1048576, folder)


# =========================================================================== отбор по тегам
_REQ_OPS = ("in", "regex")


def _required_key(clause: dict) -> Optional[str]:
    """Ключ, наличие которого обязательно для совпадения с условием (для быстрого отсева)."""
    for k, c in clause.items():
        if c is True or isinstance(c, (str, int, float, list)) and not isinstance(c, bool):
            return k
        if isinstance(c, dict):
            op, val = next(iter(c.items()))
            if op in _REQ_OPS or (op == "exists" and val):
                return k
    return None


class RecipeMatcher:
    def __init__(self, recipe: Recipe):
        self.recipe = recipe
        self.sources: list[tuple[SourceSpec, set[str], Optional[dict[str, list[dict]]]]] = []
        self.keys: Optional[set[str]] = set()
        self.border = [s for s in recipe.active_sources() if s.kind == "country_border_members"]
        for s in recipe.active_sources():
            if s.kind == "raw_overpass":
                raise ArgumentError(f"Источник {s.id} (raw_overpass) не поддерживается PBF backend",
                                    action="Используйте --backend overpass для этого рецепта")
            if s.kind != "tags":
                continue
            from .querybuilder import uses_profile
            per_country = None
            if uses_profile(s):
                per_country = {c: [{k: _resolve(v, recipe, c) for k, v in cl.items()} for cl in s.filters]
                               for c in recipe.countries}
            self.sources.append((s, set(s.element_types()), per_country))
            for cl in (s.filters or [{}]):
                k = _required_key(cl)
                if k is None:
                    self.keys = None
                elif self.keys is not None:
                    self.keys.add(k)

    def match(self, tags: dict, otype: str) -> list[tuple[str, Optional[set[str]]]]:
        out = []
        for s, types, per_country in self.sources:
            if otype not in types:
                continue
            if per_country is not None:
                cs = {c for c, cls in per_country.items() if match_any(tags, cls)}
                if cs:
                    out.append((s.id, cs))
            elif match_any(tags, s.filters):
                out.append((s.id, None))
        return out


def _ts(o) -> Optional[str]:
    try:
        return o.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return None


def _bbox_of(arr: array) -> Optional[tuple[float, float, float, float]]:
    if len(arr) < 2:
        return None
    xs, ys = arr[0::2], arr[1::2]
    return (min(xs), min(ys), max(xs), max(ys))


def _hit(b, env) -> bool:
    return b is not None and not (b[2] < env[0] or b[0] > env[2] or b[3] < env[1] or b[1] > env[3])


def extract_stores(files: list[tuple[Path, list[str]]], recipes: list[Recipe],
                   env: tuple[float, float, float, float]) -> dict[str, OSMStore]:
    """Два прохода по каждой выгрузке; возвращает {fingerprint рецепта: OSMStore}."""
    import osmium
    matchers = [RecipeMatcher(r) for r in recipes]
    keys: Optional[set[str]] = set()
    for m in matchers:
        keys = None if (keys is None or m.keys is None) else keys | m.keys
    need_border = any(m.border for m in matchers)
    border_cc = {c for m in matchers if m.border for c in m.recipe.countries}

    def pre(tg) -> bool:
        if keys is None:
            return len(tg) > 0
        return any(k in tg for k in keys)

    rels: dict[int, dict] = {}
    rel_hits: dict[int, list] = {}
    al2: dict[int, dict] = {}
    origin: dict[tuple, set[str]] = defaultdict(set)
    need_ways: set[int] = set()
    timestamps: set[str] = set()
    t_all = time.time()
    # ---------------- проход 1: relations
    for path, iso in files:
        t0 = time.time()
        try:
            hdr = osmium.io.Reader(str(path), osmium.osm.NOTHING).header()
            ts = hdr.get("osmosis_replication_timestamp")
            if ts:
                timestamps.add(ts)
        except Exception:  # noqa: BLE001
            pass
        n = 0
        for o in osmium.FileProcessor(str(path), osmium.osm.RELATION):
            tg = o.tags
            if need_border and tg.get("boundary") == "administrative" and tg.get("admin_level") == "2" \
                    and o.id not in al2:
                al2[o.id] = {"type": "relation", "id": o.id, "tags": dict(tg),
                             "members": [{"type": {"n": "node", "w": "way", "r": "relation"}[m.type],
                                          "ref": m.ref, "role": m.role} for m in o.members],
                             "version": o.version}
            if not pre(tg):
                continue
            tags = dict(tg)
            hits = [(mi, sid, cs) for mi, m in enumerate(matchers) for sid, cs in m.match(tags, "relation")]
            if not hits:
                continue
            origin[("relation", o.id)].update(iso)
            if o.id in rels:
                continue
            mem = [(m.type, m.ref, m.role) for m in o.members]
            rels[o.id] = {"tags": tags, "members": mem, "version": o.version, "timestamp": _ts(o)}
            rel_hits[o.id] = hits
            need_ways.update(ref for t, ref, _ in mem if t == "w")
            n += 1
        log.info("PBF %s: проход 1/2 (relations) — %.0f с, отобрано %d", path.name, time.time() - t0, n)
    border_ways: dict[int, set[int]] = defaultdict(set)     # way -> relations al2 стран рецептов
    for rid, el in al2.items():
        if (el["tags"].get("ISO3166-1") or "") in border_cc:
            for m in el["members"]:
                if m["type"] == "way":
                    border_ways[m["ref"]].add(rid)
    need_ways |= set(border_ways)

    # ---------------- проход 2: nodes + ways с координатами
    far = (env[0] - 1.0, env[1] - 1.0, env[2] + 1.0, env[3] + 1.0)
    ways: dict[int, dict] = {}
    wbox: dict[int, tuple] = {}
    way_hits: dict[int, list] = {}
    nodes: dict[int, dict] = {}
    node_hits: dict[int, list] = {}
    for path, iso in files:
        t0 = time.time()
        nflt = osmium.filter.KeyFilter(*sorted(keys)) if keys else osmium.filter.EmptyTagFilter()
        fp = (osmium.FileProcessor(str(path), osmium.osm.NODE | osmium.osm.WAY)
              .with_locations().with_filter(nflt.enable_for(osmium.osm.NODE)))
        nw = nn = 0
        for o in fp:
            if o.is_node():
                if o.id in nodes:
                    origin[("node", o.id)].update(iso)
                    continue
                tg = o.tags
                if not pre(tg):
                    continue
                loc = o.location
                if not loc.valid():
                    continue
                lon, lat = loc.lon, loc.lat
                if not (env[0] <= lon <= env[2] and env[1] <= lat <= env[3]):
                    continue
                tags = dict(tg)
                hits = [(mi, sid, cs) for mi, m in enumerate(matchers) for sid, cs in m.match(tags, "node")]
                if hits:
                    nodes[o.id] = {"type": "node", "id": o.id, "lat": lat, "lon": lon, "tags": tags,
                                   "version": o.version, "timestamp": _ts(o)}
                    node_hits[o.id] = hits
                    origin[("node", o.id)].update(iso)
                    nn += 1
                continue
            wid = o.id
            if wid in ways:
                origin[("way", wid)].update(iso)
                continue
            needed = wid in need_ways
            tg = o.tags
            hits = None
            if not needed:
                if not pre(tg):
                    continue
                # быстрый отсев по первому узлу: линия, начинающаяся дальше 1° от AOI, его не касается
                try:
                    loc = o.nodes[0].location
                    if loc.valid() and not (far[0] <= loc.lon <= far[2] and far[1] <= loc.lat <= far[3]):
                        continue
                except IndexError:
                    continue
            if pre(tg):
                tags = dict(tg)
                hits = [(mi, sid, cs) for mi, m in enumerate(matchers) for sid, cs in m.match(tags, "way")]
            else:
                tags = dict(tg) if needed and len(tg) else {}
            if not needed and not hits:
                continue
            arr = array("d")
            for nd in o.nodes:
                loc = nd.location
                if loc.valid():
                    arr.append(loc.lon)
                    arr.append(loc.lat)
            b = _bbox_of(arr)
            inside = _hit(b, env)
            if not needed and not inside:
                continue
            el = {"type": "way", "id": wid, "geometry": arr, "version": o.version, "timestamp": _ts(o)}
            if tags:
                el["tags"] = tags
            ways[wid] = el
            wbox[wid] = b
            origin[("way", wid)].update(iso)
            if hits and inside:
                way_hits[wid] = hits
            nw += 1
        del fp
        log.info("PBF %s: проход 2/2 (узлы и линии) — %.0f с, линий %d, точек %d", path.name,
                 time.time() - t0, nw, nn)

    # ---------------- сборка relations
    rel_el: dict[int, dict] = {}
    incomplete: set[int] = set()
    for rid, r in rels.items():
        members, box, miss = [], None, 0
        for t, ref, role in r["members"]:
            if t == "w":
                m = {"type": "way", "ref": ref, "role": role}
                w = ways.get(ref)
                if w is not None and len(w["geometry"]) >= 4:
                    m["geometry"] = w["geometry"]
                    b = wbox.get(ref)
                    if b:
                        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                                     max(box[2], b[2]), max(box[3], b[3]))
                elif role in ("outer", "inner", ""):
                    miss += 1
                members.append(m)
            elif t == "n":
                m = {"type": "node", "ref": ref, "role": role}
                nd = nodes.get(ref)
                if nd is not None:
                    m["lat"], m["lon"] = nd["lat"], nd["lon"]
                members.append(m)
            else:
                members.append({"type": "relation", "ref": ref, "role": role})
        if not _hit(box, env):
            continue              # relation вне AOI (или без геометрии в скачанных выгрузках)
        el = {"type": "relation", "id": rid, "tags": r["tags"], "members": members,
              "version": r["version"], "timestamp": r["timestamp"]}
        if box is not None:
            el["_bbox"] = box
        rel_el[rid] = el
        if miss:
            incomplete.add(rid)

    # ---------------- хранилища рецептов
    out: dict[str, OSMStore] = {}
    for mi, m in enumerate(matchers):
        st = OSMStore()
        st.data_timestamps |= timestamps
        st.pbf_origin = {}
        st.pbf_incomplete = defaultdict(set)
        geo_out = {s.id: s.geometry_output for s, _, _ in m.sources}

        def put(key, el, sid, cs):
            go = geo_out.get(sid, "full")
            if go != "full" and key[0] != "node":
                el = _reduced(el, go, wbox.get(key[1]) if key[0] == "way" else el.get("_bbox"))
            st._put(el)
            st.members[sid].setdefault(key, set())
            if cs is not None:
                st.candidates[sid].setdefault(key, set()).update(cs)
            st.pbf_origin[key] = origin.get(key, set())

        for nid, hits in node_hits.items():
            for h_mi, sid, cs in hits:
                if h_mi == mi:
                    put(("node", nid), nodes[nid], sid, cs)
        for wid, hits in way_hits.items():
            for h_mi, sid, cs in hits:
                if h_mi == mi:
                    put(("way", wid), ways[wid], sid, cs)
        for rid, hits in rel_hits.items():
            el = rel_el.get(rid)
            if el is None:
                continue
            for h_mi, sid, cs in hits:
                if h_mi == mi:
                    put(("relation", rid), el, sid, cs)
                    if rid in incomplete and geo_out.get(sid) != "tags":
                        st.pbf_incomplete[sid].add(rid)
        for s in m.border:
            cc = set(m.recipe.countries)
            parents: set[int] = set()
            for wid, rset in border_ways.items():
                mine = {r for r in rset if (al2[r]["tags"].get("ISO3166-1") or "") in cc}
                if not mine or wid not in ways or not _hit(wbox.get(wid), env):
                    continue
                key = ("way", wid)
                st._put(ways[wid])
                st.members[s.id].setdefault(key, set())
                st.pbf_origin[key] = origin.get(key, set())
                parents.add(wid)
            for rid, el in al2.items():
                if any(mm["type"] == "way" and mm["ref"] in parents for mm in el["members"]):
                    st._put(el)
                    st.border_parents[s.id].add(rid)
        out[recipe_fingerprint(m.recipe)] = st
    log.info("PBF: отбор объектов для %d рецептов — %.0f с", len(recipes), time.time() - t_all)
    return out


def _reduced(el: dict, go: str, box) -> dict:
    """geometry_output center/tags: как out center / out tags в Overpass."""
    new = {k: v for k, v in el.items() if k in ("type", "id", "tags", "version", "timestamp")}
    if go == "center" and box is not None:
        new["center"] = {"lon": (box[0] + box[2]) / 2, "lat": (box[1] + box[3]) / 2}
    return new


# =========================================================================== исполнитель
def aoi_key(aoi: AOIModel, extracts: Iterable[Extract]) -> str:
    w, s, e, n = aoi.envelope_wsen
    raw = f"{w:.5f},{s:.5f},{e:.5f},{n:.5f}|" + ",".join(sorted(x.id for x in extracts))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


class PBFExecutor(Executor):
    """Данные — из PBF; Overpass используется только для лёгких вспомогательных запросов."""

    def __init__(self, config, provider, cache, recipe: Recipe, header: str, aoi: AOIModel,
                 target: Path, batch_recipes: Optional[list[Recipe]] = None):
        super().__init__(config, provider, cache, recipe, header)
        self.aoi = aoi
        self.folder = pbf_dir_for(target)
        self.batch_recipes = batch_recipes or []
        self.extracts: list[Extract] = []
        self.source_desc = ""

    def _env(self) -> tuple[float, float, float, float]:
        w, s, e, n = self.aoi.envelope_wsen
        d = 1e-4
        return (w - d, s - d, e + d, n + d)

    def load_store(self) -> OSMStore:
        require_osmium()
        feats = load_index(self.config)
        self.extracts = select_extracts(_aoi_shape(self.aoi), feats)
        self.source_desc = "Geofabrik: " + ", ".join(x.url for x in self.extracts)
        key = aoi_key(self.aoi, self.extracts)
        fp = recipe_fingerprint(self.recipe)
        mine = self.folder / f"store_{fp[:16]}_{key}.pkl"
        if mine.exists():
            log.info("PBF: данные рецепта уже отобраны в этом пакете — %s", mine.name)
            with open(mine, "rb") as f:
                return pickle.load(f)
        log.info("PBF: выгрузки Geofabrik для AOI: %s", ", ".join(x.id for x in self.extracts))
        files = [(download(x, self.folder), x.iso) for x in self.extracts]
        recipes, seen = [], set()
        for r in [self.recipe] + list(self.batch_recipes):
            f = recipe_fingerprint(r)
            if f not in seen and not (self.folder / f"store_{f[:16]}_{key}.pkl").exists():
                seen.add(f)
                recipes.append(r)
        if len(recipes) > 1:
            log.info("PBF: отбор сразу для %d рецептов пакета: %s", len(recipes), ", ".join(r.id for r in recipes))
        stores = extract_stores(files, recipes, self._env())
        result = None
        for r in recipes:
            f = recipe_fingerprint(r)
            st = stores[f]
            if f == fp:
                result = st
            if r is not self.recipe or self.batch_recipes:
                p = self.folder / f"store_{f[:16]}_{key}.pkl"
                with open(p.with_suffix(".tmp"), "wb") as fh:
                    pickle.dump(st, fh, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(p.with_suffix(".tmp"), p)
        return result

    # ------------------------------------------------------------------ страны
    def _country_geom(self, key):
        from shapely.geometry import MultiLineString, Point
        from shapely.ops import polygonize, unary_union
        from .parsers.osm_geometry import coords_of
        el = self.store.elements.get(key)
        if not el:
            return None
        if "lat" in el and "lon" in el:
            return Point(el["lon"], el["lat"])
        if "center" in el:
            return Point(el["center"]["lon"], el["center"]["lat"])
        seqs = [el["geometry"]] if el.get("geometry") is not None else []
        seqs += [m["geometry"] for m in el.get("members") or [] if m.get("geometry") is not None]
        cs = [coords_of(g) for g in seqs]
        cs = [c for c in cs if len(c) >= 2]
        if not cs:
            return None
        lines = MultiLineString(cs)
        if el.get("members") and (el.get("tags") or {}).get("type") in ("boundary", "multipolygon"):
            try:
                polys = list(polygonize(unary_union(lines)))
                if polys:
                    return unary_union(polys + [lines])
            except Exception:  # noqa: BLE001
                pass
        return lines

    def assign_countries(self) -> None:
        if not self.scoped_ids:
            return
        import shapely
        from shapely.strtree import STRtree
        targets = set(self.recipe.countries)
        keys = sorted({k for sid in self.scoped_ids for k in self.store.members.get(sid, {})})
        found: dict[tuple, set[str]] = {}
        if self.tile_faces:
            tiles = list(self.tile_faces.values())
            boxes = []
            for faces in tiles:
                bounds = shapely.union_all([f for f, _ in faces]).bounds
                boxes.append(shapely.box(*bounds))
            ttree = STRtree(boxes)
            all_faces = [fc for faces in tiles for fc in faces]
            polys = [f for f, _ in all_faces]
            inner = [f.buffer(-1e-7) for f in polys]
            ftree = STRtree(polys)
            obj_boxes = []
            for k in keys:
                g = None
                el = self.store.elements.get(k) or {}
                if "lat" in el:
                    b = (el["lon"], el["lat"], el["lon"], el["lat"])
                elif "center" in el:
                    b = (el["center"]["lon"], el["center"]["lat"]) * 2
                elif k[0] == "way" and isinstance(el.get("geometry"), array):
                    b = _bbox_of(el["geometry"])
                else:
                    b = el.get("_bbox")
                if b is None:
                    g = self._country_geom(k)
                    b = g.bounds if g is not None and not g.is_empty else None
                obj_boxes.append(shapely.box(*b) if b else None)
            simple: dict[int, set[str]] = {}
            valid = [i for i, b in enumerate(obj_boxes) if b is not None]
            if valid:
                arr = [obj_boxes[i] for i in valid]
                pairs = ttree.query(arr, predicate="within")
                for a, t in zip(pairs[0], pairs[1]):
                    faces = tiles[t]
                    if len(faces) == 1:
                        simple[valid[a]] = set(faces[0][1])
            for i, k in enumerate(keys):
                if i in simple:
                    found[k] = simple[i]
                    continue
                if obj_boxes[i] is None:
                    continue
                g = self._country_geom(k)
                if g is None:
                    continue
                cand = [j for j in ftree.query(g) if polys[j].intersects(g)]
                strict = [j for j in cand if inner[j].intersects(g)]
                cs: set[str] = set()
                for j in (strict or cand):
                    cs.update(all_faces[j][1])
                if cand:
                    found[k] = cs
        origin = getattr(self.store, "pbf_origin", {})
        n_fallback = 0
        for sid in self.scoped_ids:
            mem = self.store.members.get(sid, {})
            cands = self.store.candidates.get(sid, {})
            for k in list(mem):
                cs = found.get(k)
                if cs is None:          # грани не определены — страна по выгрузке Geofabrik
                    cs = set(origin.get(k, set())) & targets
                    if cs:
                        n_fallback += 1
                if k in cands:
                    cs = cs & cands[k]
                if cs:
                    mem[k] = set(cs)
                else:
                    del mem[k]
        if n_fallback:
            log.warning("PBF: для %d объектов страна определена по выгрузке Geofabrik (приблизительно "
                        "у госграниц)", n_fallback)

    def run(self, tiles: list[BBox]) -> OSMStore:
        sources = self.recipe.active_sources()
        self.store = self.load_store()
        self.classify_tiles(tiles, sources, force=True)
        if not self.tile_faces and self.scoped_ids and self.recipe.countries:
            log.warning("PBF: линии госграниц не получены — страна объектов по выгрузкам Geofabrik")
        self.assign_countries()
        for sid, rids in sorted(getattr(self.store, "pbf_incomplete", {}).items()):
            rids = sorted(r for r in rids if ("relation", r) in self.store.members.get(sid, {}))
            if not rids:
                continue
            log.info("%s: %d relations выходят за пределы выгрузок — догрузка целиком через Overpass",
                     sid, len(rids))
            try:
                self.fetch_relations(sid, rids)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: relations не догружены (%s) — контуры могут быть неполными", sid, exc)
                for r in rids:
                    self.store.warn("W_PBF_INCOMPLETE", "", f"r{r}",
                                    "часть членов relation вне скачанных выгрузок; Overpass недоступен")
        for s in sources:
            if s.country_resolution == "point":
                try:
                    self.resolve_countries(s.id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s: уточнение страны через is_in не выполнено (%s)", s.id, exc)
        if any(o.transform == "country_outlines" for o in self.recipe.active_outputs()):
            from .querybuilder import country_outline_query
            log.warning("Загрузка полных полигонов стран через Overpass — тяжёлый запрос")
            self.store.ingest(self._run(country_outline_query(self.recipe.countries, self.header),
                                        "country outlines"), count_occurrences=False)
        return self.store
