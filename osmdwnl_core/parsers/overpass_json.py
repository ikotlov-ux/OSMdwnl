"""Разбор Overpass JSON (это не GeoJSON) во внутреннее хранилище объектов с дедупликацией."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from ..errors import ParseError
from ..querybuilder import MARKER

Key = tuple[str, int]   # (osm_type, osm_id)


def _completeness(el: dict) -> int:
    score = 0
    if "geometry" in el or "lat" in el or "center" in el:
        score += 2
    if el.get("type") == "relation":
        mem = el.get("members") or []
        score += sum(1 for m in mem if m.get("geometry") or "lat" in m)
        if mem:
            score += 1
    if "tags" in el:
        score += 1
    if "version" in el:
        score += 1
    return score


class OSMStore:
    """Хранилище OSM-объектов, собранных из всех тайлов и физических запросов."""

    def __init__(self):
        self.elements: dict[Key, dict] = {}
        self.occurrences: Counter = Counter()
        # source_id -> key -> set(country); country '' — без страновой привязки
        self.members: dict[str, dict[Key, set[str]]] = defaultdict(dict)
        self.pending_relations: dict[str, set[int]] = defaultdict(set)
        self.border_parents: dict[str, set[int]] = defaultdict(set)   # source -> relation ids
        self.candidates: dict[str, dict] = defaultdict(dict)          # source -> key -> страны
        self.is_in: dict[str, set[str]] = defaultdict(set)            # uid -> ISO codes
        self.warnings: list[tuple[str, str, str, str]] = []           # code, layer, uid, text
        self.data_timestamps: set[str] = set()                        # osm3s.timestamp_osm_base

    def warn(self, code: str, layer: str, uid: str, text: str) -> None:
        self.warnings.append((code, layer, uid, text))

    # ------------------------------------------------------------
    def _put(self, el: dict) -> Key:
        key = (el["type"], int(el["id"]))
        old = self.elements.get(key)
        if old is None:
            self.elements[key] = el
            return key
        ov, nv = old.get("version", -1), el.get("version", -1)
        if nv > ov:
            self.elements[key] = el
        elif nv == ov:
            if el.get("tags") and old.get("tags") and el["tags"] != old["tags"]:
                self.warn("W_TAG_CONFLICT", "", uid_of(key),
                          "одинаковая версия объекта пришла с разными тегами из разных тайлов")
            if _completeness(el) > _completeness(old):
                merged = dict(el)
                if "tags" not in merged and "tags" in old:
                    merged["tags"] = old["tags"]
                self.elements[key] = merged
        return key

    def ingest(self, data: dict[str, Any], count_occurrences: bool = True) -> None:
        elements = data.get("elements")
        if not isinstance(elements, list):
            raise ParseError("в ответе Overpass нет списка elements")
        ts = (data.get("osm3s") or {}).get("timestamp_osm_base")
        if ts:
            self.data_timestamps.add(ts)
        source, kind, country = None, None, ""
        for el in elements:
            for private in ("user", "uid", "changeset"):   # персональные данные не храним
                el.pop(private, None)
            t = el.get("type")
            if t == MARKER:
                tg = el.get("tags") or {}
                source, kind, country = tg.get("source"), tg.get("kind"), tg.get("country", "")
                continue
            if t not in ("node", "way", "relation", "area") or "id" not in el:
                continue
            if kind == "is_in":
                if t == "area":
                    iso = (el.get("tags") or {}).get("ISO3166-1")
                    if iso:
                        self.is_in[source].add(iso)
                continue
            if t == "area":
                continue
            key = (t, int(el["id"]))
            if kind == "ids":
                self.members[source].setdefault(key, set()).add(country)
                continue
            if kind == "cand":     # прошёл фильтр страны ($profile); страна уточняется по граням
                self.candidates[source].setdefault(key, set()).add(country)
                continue
            if kind == "rel_ids":
                if t == "relation":
                    self.members[source].setdefault(key, set())
                    if key not in self.elements or not _has_geom(self.elements[key]):
                        self.pending_relations[source].add(int(el["id"]))
                continue
            if kind == "parents":
                self._put(el)
                self.border_parents[source].add(int(el["id"]))
                continue
            # kind == data (или поток без маркеров)
            self._put(el)
            if count_occurrences:
                self.occurrences[key] += 1
            if source is not None:
                self.members[source].setdefault(key, set())
                if t == "relation" and _has_geom(el):
                    self.pending_relations[source].discard(int(el["id"]))

    def keys_of(self, source: str) -> dict[Key, set[str]]:
        return self.members.get(source, {})

    def missing_relations(self, source: str) -> list[int]:
        return sorted(rid for rid in self.pending_relations.get(source, set())
                      if not _has_geom(self.elements.get(("relation", rid), {})))


def _has_geom(el: dict) -> bool:
    if not el:
        return False
    if el.get("type") == "relation":
        return any(m.get("geometry") or "lat" in m for m in el.get("members") or [])
    return "geometry" in el or "lat" in el or "center" in el


def uid_of(key: Key) -> str:
    return {"node": "n", "way": "w", "relation": "r"}[key[0]] + str(key[1])


def tags_json(tags: dict) -> str:
    return json.dumps(tags, ensure_ascii=False, sort_keys=True)
