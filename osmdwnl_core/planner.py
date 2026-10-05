"""Планировщик и исполнитель: тайлинг, рекурсивное дробление, двухэтапная загрузка relations."""
from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .cache import ResponseCache, query_hash
from .errors import ArgumentError, NetworkError, QueryTooLarge
from .models import AOIModel, BBox, ExecutionPlan, PhysicalQuery
from .parsers.osm_geometry import coords_of
from .parsers.overpass_json import OSMStore
from .querybuilder import (border_probe_query, country_outline_query, is_in_query, relations_query,
                           tile_query)
from .recipes import Recipe
from .transforms.boundaries import resolve_points

log = logging.getLogger("osmdwnl")


def choose_backend(requested: str, aoi: AOIModel, config, force: bool = False) -> tuple[str, list[str]]:
    notes = []
    area = sum(b.area_deg2 for b in aoi.bboxes)
    if requested in ("pbf", "postgis"):
        return requested, notes
    if area > config.planner.recommend_pbf_area_deg2:
        notes.append(f"Площадь bbox {area:.1f} град² — для такой территории быстрее PBF "
                     f"(--backend pbf); будет использован Overpass с тайлингом")
    if area > config.planner.overpass_hard_limit_deg2 and not force:
        raise ArgumentError(
            f"AOI слишком велик для публичного Overpass ({area:.0f} град² > "
            f"{config.planner.overpass_hard_limit_deg2:.0f})",
            action="Уменьшите AOI, увеличьте planner.overpass_hard_limit_deg2 или добавьте --force")
    return "overpass", notes


class QueryPlanner:
    def __init__(self, config, cache: Optional[ResponseCache] = None):
        self.config = config
        self.cache = cache

    def max_tile_area(self, recipe: Recipe) -> float:
        return recipe.tiling.max_bbox_area_deg2 or self.config.tiling.max_bbox_area_deg2

    def tiles(self, aoi: AOIModel, recipe: Recipe) -> list[BBox]:
        enabled = recipe.tiling.enabled if recipe.tiling.enabled is not None else self.config.tiling.enabled
        out = []
        for b in aoi.bboxes:
            out.extend(b.grid(self.max_tile_area(recipe)) if enabled else [b])
        if aoi.mode == "geometry" and len(out) > 1:
            g = aoi.geometry
            out = [t for t in out if t.polygon().intersects(g)]
        return out

    def plan(self, aoi: AOIModel, recipe: Recipe, backend: str, header: str, endpoint: str) -> ExecutionPlan:
        srcs = recipe.active_sources()
        queries = [PhysicalQuery(purpose=f"тайл {i + 1}", query=tile_query(recipe, srcs, t, header), tile=t)
                   for i, t in enumerate(self.tiles(aoi, recipe))]
        plan = ExecutionPlan(recipe_id=recipe.id, backend=backend, endpoint=endpoint,
                             tiles=[q.tile for q in queries], queries=queries,
                             layers=[o.layer for o in recipe.active_outputs()])
        if any(s.geometry_output == "full" and "relation" in s.element_types() and s.kind == "tags"
               for s in srcs):
            plan.notes.append("Этап 2: relations, найденные в тайлах, загружаются пакетами по ID со всеми членами")
        if (self.config.overpass.smart_country_filter and len(recipe.countries) > 0
                and any(s.kind == "tags" and s.country_scope == "area" for s in srcs)):
            plan.notes.append("Ускорение: тайлы без государственной границы запрашиваются только по bbox "
                              "(страна по is_in центра тайла); фильтр по areas стран — только на тайлах с границей")
        if any(s.country_resolution == "point" for s in srcs):
            plan.notes.append("Этап 3: объекты на стыке стран уточняются запросом is_in по внутренней точке")
        if any(o.transform == "country_outlines" for o in recipe.active_outputs()):
            plan.notes.append("Дополнительно: полные полигоны стран (тяжёлый запрос)")
        return plan


class Executor:
    def __init__(self, config, provider, cache: ResponseCache, recipe: Recipe, header: str):
        self.config = config
        self.provider = provider
        self.cache = cache
        self.recipe = recipe
        self.header = header
        self.store = OSMStore()
        self.queries_log: list[dict] = []
        self.tile_faces: dict[tuple, list] = {}     # ключ bbox -> [(грань, [страны])]
        self.local_hits: list = []                  # (грани, [(source, key)]) тайлов на границе
        self.scoped_ids = [s.id for s in recipe.active_sources()
                           if s.kind == "tags" and s.country_scope == "area"]
        # «лёгкий» заголовок для тайлов/границ/is_in, полный — для relations целиком
        lh = getattr(provider, "light_header", None)
        self.light = lh() if callable(lh) else header
        self._lock = threading.Lock()
        self._tl = threading.local()

    def _prov(self):
        return getattr(self._tl, "provider", None) or self.provider

    def _run(self, query: str, purpose: str, split_after_busy: int = 0):
        if self.light != self.header and query.startswith(self.light) and not self._cached_any(query):
            legacy = self.header + query[len(self.light):]
            if self._cached_any(legacy):       # ответ, полученный версией ≤ 1.6 с полным заголовком
                query = legacy
        prov = self._prov()
        if split_after_busy:
            data = prov.execute(query, purpose, split_after_busy=split_after_busy)
        else:
            data = prov.execute(query, purpose)
        with self._lock:
            self.queries_log.append({"purpose": purpose, "hash": query_hash(query), "query": query})
        return data

    # --------------------------------------------------- параллельно на разных зеркалах
    def workers_count(self, n_jobs: int) -> int:
        if not hasattr(self.provider, "clone"):
            return 1
        cfg = self.provider.cfg
        return max(1, min(int(getattr(cfg, "concurrency", 1)), len(cfg.endpoints), n_jobs))

    def _parallel(self, jobs: list[Callable[[], None]]) -> None:
        n = self.workers_count(len(jobs))
        if n <= 1:
            for job in jobs:
                job()
            return
        provs: "queue.Queue" = queue.Queue()
        provs.put(self.provider)
        for i in range(1, n):
            provs.put(self.provider.clone(self.provider._ep + i))

        def init():
            self._tl.provider = provs.get()

        log.info("Параллельная загрузка: %d потока, у каждого своё зеркало", n)
        with ThreadPoolExecutor(max_workers=n, initializer=init) as pool:
            futures = [pool.submit(job) for job in jobs]
            try:
                for f in futures:
                    f.result()
            except BaseException:
                for f in futures:
                    f.cancel()
                raise

    # --------------------------------------------------- ускорение: страна без фильтра по areas
    # Фильтр (area.c_RU) заставляет Overpass обрабатывать полигон всей страны (~50 с на тайл).
    # Вместо него: линии госграниц в AOI (один лёгкий запрос) делят каждый тайл на грани;
    # страна каждой грани — is_in её внутренней точки (пакетно). Тайл с одной гранью
    # запрашивается по bbox с готовой страной; тайл с несколькими гранями — по bbox, а страна
    # объекта = страны граней, которые он пересекает (как у area-фильтра: «любая часть внутри»).
    @staticmethod
    def _tkey(t: BBox) -> tuple:
        return (round(t.w, 7), round(t.s, 7), round(t.e, 7), round(t.n, 7))

    def classify_tiles(self, tiles: list[BBox], sources, force: bool = False) -> None:
        if not ((force or self.config.overpass.smart_country_filter) and self.recipe.countries and tiles
                and any(s.kind == "tags" and s.country_scope == "area" for s in sources)):
            return
        from shapely.geometry import LineString
        from shapely.ops import polygonize, unary_union
        from shapely.strtree import STRtree
        env = BBox(min(t.w for t in tiles), min(t.s for t in tiles),
                   max(t.e for t in tiles), max(t.n for t in tiles))
        try:
            data = self._run(border_probe_query(self.recipe.countries, env, self.light),
                             "границы стран в AOI")
        except Exception as exc:  # noqa: BLE001 — ускорение необязательно
            log.warning("Линии госграниц не получены (%s)%s", exc, "" if force else " — фильтр по areas стран")
            return
        lines = []
        for el in data.get("elements", []):
            pts = coords_of(el.get("geometry"))
            if len(pts) >= 2:
                lines.append(LineString(pts))
        tree = STRtree(lines) if lines else None
        faces_of: dict[tuple, list] = {}
        for t in tiles:
            poly = t.polygon()
            hit = [lines[i] for i in tree.query(poly)] if tree is not None else []
            hit = [ln for ln in hit if ln.intersects(poly)]
            if not hit:
                faces_of[self._tkey(t)] = [poly]
                continue
            noded = unary_union([poly.boundary] + [ln.intersection(poly) for ln in hit])
            faces = [f for f in polygonize(noded) if f.area > 1e-10]
            faces_of[self._tkey(t)] = faces or [poly]
        pts, owners = [], []
        for k, faces in faces_of.items():
            for j, f in enumerate(faces):
                rp = f.representative_point()
                pts.append((f"f{len(pts)}", rp.x, rp.y))
                owners.append((k, j))
        probe = OSMStore()
        resolved: set[str] = set()
        step = 25
        for start in range(0, len(pts), step):
            batch = pts[start:start + step]
            try:
                probe.ingest(self._run(is_in_query(batch, self.light),
                                       f"страна частей тайлов {start + 1}-{start + len(batch)} из {len(pts)}"),
                             count_occurrences=False)
                resolved.update(u for u, _, _ in batch)
            except Exception as exc:  # noqa: BLE001 — эти тайлы пойдут с фильтром по areas
                log.warning("is_in для частей %d-%d не выполнен (%s): эти тайлы — с фильтром по areas",
                            start + 1, start + len(batch), exc)
        targets = list(self.recipe.countries)
        result: dict[tuple, list] = {}
        bad: set[tuple] = set()
        for (uid, _, _), (k, j) in zip(pts, owners):
            if uid not in resolved:
                bad.add(k)
                continue
            cs = [c for c in targets if c in probe.is_in.get(uid, set())]
            result.setdefault(k, []).append((faces_of[k][j], cs))
        for k in bad:
            result.pop(k, None)
        self.tile_faces = result
        n_split = sum(1 for v in result.values() if len(v) > 1)
        log.info("Страны без фильтра по areas: тайлов %d из %d, из них на границе %d (частей: %d)",
                 len(result), len(tiles), n_split, sum(len(v) for v in result.values() if len(v) > 1))

    def _cached_any(self, q: str) -> bool:
        cfg = getattr(self.provider, "cfg", None)
        eps = getattr(cfg, "endpoints", None) or [getattr(self.provider, "endpoint", "")]
        tag = getattr(self.provider, "recipe_tag", "")
        return any(self.cache.get(query_hash(q, ep, tag)) is not None for ep in eps)

    def assign_local_countries(self) -> None:
        """Страна объектов из тайлов на границе — по пересечению с гранями тайла."""
        if not self.local_hits:
            return
        from shapely.geometry import LineString, MultiLineString, Point
        from shapely.strtree import STRtree

        def geom(key):
            el = self.store.elements.get(key)
            if not el:
                return None
            if "lat" in el and "lon" in el:
                return Point(el["lon"], el["lat"])
            seqs = []
            if el.get("geometry"):
                seqs.append(el["geometry"])
            for m in el.get("members") or []:
                if m.get("geometry"):
                    seqs.append(m["geometry"])
            ls = [LineString(coords_of(g)) for g in seqs if len(coords_of(g)) >= 2]
            ls = [x for x in ls if len(x.coords) >= 2]
            if not ls:
                return None
            lines = MultiLineString(ls)
            if el.get("members") and (el.get("tags") or {}).get("type") in ("boundary", "multipolygon"):
                # контур relation как полигон: грань внутри большого района/области (без линий
                # его границы) тоже должна с ним пересекаться
                try:
                    from shapely.ops import polygonize, unary_union
                    polys = list(polygonize(unary_union(ls)))
                    if polys:
                        return unary_union(polys + [lines])
                except Exception:   # noqa: BLE001 — некорректный контур: остаются линии
                    pass
            return lines

        cache: dict = {}
        for faces, hits in self.local_hits:
            polys = [f for f, _ in faces]
            inner = [f.buffer(-1e-7) for f in polys]     # без касания по линии границы
            tree = STRtree(polys)
            for source, key in hits:
                if key not in cache:
                    cache[key] = geom(key)
                g = cache[key]
                if g is None:
                    continue
                cand = [i for i in tree.query(g) if polys[i].intersects(g)]
                strict = [i for i in cand if inner[i].intersects(g)]
                found = set()
                # объект, лежащий только на линии границы (река-граница), относится к обеим странам
                for i in (strict or cand):
                    found.update(faces[i][1])
                allowed = self.store.candidates.get(source, {}).get(key)
                if allowed is not None:          # фильтр с $profile: только страны, где он прошёл
                    found &= allowed
                self.store.members[source].setdefault(key, set()).update(found)
        # объекты, не попавшие ни в одну страну рецепта (аналог отсечения area-фильтром)
        for sid in self.scoped_ids:
            mem = self.store.members.get(sid, {})
            for key in [k for k, v in mem.items() if not v]:
                del mem[key]

    def fetch_tile(self, tile: BBox, sources, depth: int = 0, label: str = "тайл",
                   faces: list | None = None) -> None:
        if faces is None:
            faces = self.tile_faces.get(self._tkey(tile))
        hdr = self.light
        if faces is None:
            q = tile_query(self.recipe, sources, tile, hdr)
        elif len(faces) == 1:
            q = tile_query(self.recipe, sources, tile, hdr, tile_countries=faces[0][1])
        else:
            q = tile_query(self.recipe, sources, tile, hdr, local_countries=True)
        if faces is not None:
            q_area = tile_query(self.recipe, sources, tile, hdr)
            if self._cached_any(q_area):      # тайл уже загружен прежней версией — берём из кэша
                q, faces = q_area, None
        failures = self.cache.failures()
        known_bad = failures.get(query_hash(q, self._prov().endpoint, getattr(self.provider, "recipe_tag", "")), 0)
        if known_bad and depth < self.config.tiling.max_split_depth:
            log.info("%s: ранее превышал лимиты — сразу делится на 4", label)
            for i, sub in enumerate(tile.quadrants()):
                self.fetch_tile(sub, sources, depth + 1, f"{label}.{i + 1}", faces)
            return
        can_split = depth < self.config.tiling.max_split_depth
        split_busy = int(getattr(getattr(self.provider, "cfg", None), "split_after_busy", 0) or 0)
        try:
            data = self._run(q, f"{label} [{tile.overpass()}]", split_busy if can_split else 0)
        except QueryTooLarge as exc:
            if depth >= self.config.tiling.max_split_depth:
                raise NetworkError(f"{label}: запрос слишком тяжёлый даже после {depth} делений",
                                   cause=exc.cause, action="Уменьшите AOI или используйте PBF") from exc
            log.warning("%s: %s — делю на 4 части (глубина %d)", label, exc.cause, depth + 1)
            for i, sub in enumerate(tile.quadrants()):
                self.fetch_tile(sub, sources, depth + 1, f"{label}.{i + 1}", faces)
            return
        with self._lock:
            if faces is not None and len(faces) > 1:
                tmp = OSMStore()
                tmp.ingest(data, count_occurrences=False)
                self.local_hits.append((faces, [(sid, k) for sid in self.scoped_ids
                                                for k in set(tmp.members.get(sid, {}))
                                                | set(tmp.candidates.get(sid, {}))]))
            self.store.ingest(data)

    def fetch_relations(self, source_id: str, ids: list[int], depth: int = 0) -> None:
        if not ids:
            return
        size = self.config.overpass.relation_batch_size
        if depth == 0 and size != 40:
            # пакеты по 40 из кэша прежних версий (1.0–1.2.1) — повторно не загружаем
            rest = []
            for st in range(0, len(ids), 40):
                b = ids[st:st + 40]
                q = relations_query(source_id, b, self.header)
                if self._cached_any(q):
                    data = self._run(q, f"relations {source_id} (кэш)")
                    with self._lock:
                        self.store.ingest(data, count_occurrences=False)
                else:
                    rest += b
            if len(rest) < len(ids):
                log.info("relations %s: %d из кэша, загрузить %d", source_id, len(ids) - len(rest), len(rest))
            ids = rest
        def one(start: int, batch: list[int]) -> None:
            q = relations_query(source_id, batch, self.header)
            try:
                data = self._run(q, f"relations {source_id} {start + 1}-{start + len(batch)} из {len(ids)}")
            except (QueryTooLarge, NetworkError) as exc:
                # тяжёлый пакет или сервер не справился — делим пакет пополам и продолжаем
                if len(batch) <= 2 or depth > 8:
                    raise
                log.warning("relations %s: пакет %d не загружен (%s) — делю пополам",
                            source_id, len(batch), exc.message if hasattr(exc, "message") else exc)
                half = len(batch) // 2
                self.fetch_relations(source_id, batch[:half], depth + 1)
                self.fetch_relations(source_id, batch[half:], depth + 1)
                return
            with self._lock:
                self.store.ingest(data, count_occurrences=False)

        jobs = [(lambda st=st: one(st, ids[st:st + size])) for st in range(0, len(ids), size)]
        if depth == 0:
            self._parallel(jobs)
        else:
            for job in jobs:
                job()

    def run(self, tiles: list[BBox]) -> OSMStore:
        sources = self.recipe.active_sources()
        self.classify_tiles(tiles, sources)
        self._parallel([(lambda i=i, t=t: self.fetch_tile(t, sources, label=f"тайл {i + 1}/{len(tiles)}"))
                        for i, t in enumerate(tiles)])
        for s in sources:
            if s.kind == "tags" and s.geometry_output == "full" and "relation" in s.element_types():
                missing = self.store.missing_relations(s.id)
                if missing:
                    log.info("%s: загрузка %d relations со всеми членами", s.id, len(missing))
                    self.fetch_relations(s.id, missing)
        if self.tile_faces:
            self.assign_local_countries()
        for s in sources:
            if s.country_resolution == "point":
                self.resolve_countries(s.id)
        if any(o.transform == "country_outlines" for o in self.recipe.active_outputs()):
            log.warning("Загрузка полных полигонов стран — тяжёлый запрос")
            self.store.ingest(self._run(country_outline_query(self.recipe.countries, self.header),
                                        "country outlines"), count_occurrences=False)
        return self.store

    def resolve_countries(self, source_id: str) -> None:
        pts = resolve_points(self.store, self.recipe, source_id)
        if not pts:
            return
        log.info("%s: уточнение страны для %d объектов на стыке стран", source_id, len(pts))
        for start in range(0, len(pts), 50):
            batch = pts[start:start + 50]
            self.store.ingest(self._run(is_in_query(batch, self.light), f"is_in {source_id}"),
                              count_occurrences=False)
        uid_map = {u: None for u, _, _ in pts}
        members = self.store.members[source_id]
        for key in list(members):
            uid = {"node": "n", "way": "w", "relation": "r"}[key[0]] + str(key[1])
            if uid in uid_map:
                found = self.store.is_in.get(uid, set()) & set(self.recipe.countries)
                if found:
                    members[key] = found
                else:
                    self.store.warn("W_COUNTRY_UNRESOLVED", "", uid,
                                    "страна не определена по внутренней точке; сохранён список стран")
