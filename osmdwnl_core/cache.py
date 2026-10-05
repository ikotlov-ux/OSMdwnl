"""Кэш ответов (.json.gz) по SHA-256 нормализованного запроса, endpoint и версии рецепта."""
from __future__ import annotations

import gzip
import hashlib
import json
import threading
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional


_FAIL_LOCK = threading.Lock()


def normalize_query(q: str) -> str:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in q.strip().splitlines()]
    return "\n".join(ln for ln in lines if ln and not ln.startswith("//"))


def query_hash(query: str, endpoint: str = "", recipe_tag: str = "") -> str:
    h = hashlib.sha256()
    h.update(normalize_query(query).encode("utf-8"))
    h.update(b"\x00" + endpoint.encode("utf-8") + b"\x00" + recipe_tag.encode("utf-8"))
    return h.hexdigest()


class ResponseCache:
    def __init__(self, root: Path, enabled: bool = True, ttl_days: float = 30):
        self.root = Path(root)
        self.enabled = enabled
        self.ttl = ttl_days * 86400
        self.hits = 0
        self.misses = 0
        self.used_keys: list[str] = []
        if enabled:
            (self.root / "responses").mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / "responses" / key[:2] / f"{key}.json.gz"

    def get(self, key: str) -> Optional[Any]:
        if not self.enabled:
            return None
        p = self._path(key)
        if not p.exists():
            self.misses += 1
            return None
        if self.ttl > 0 and time.time() - p.stat().st_mtime > self.ttl:
            self.misses += 1
            return None
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            self.misses += 1
            return None
        self.hits += 1
        self.used_keys.append(key)
        return data

    def put(self, key: str, data: Any) -> None:
        if not self.enabled:
            return
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
        self.used_keys.append(key)

    # --- история неудачных запросов: планировщик сразу дробит такие тайлы ---
    def _fail_path(self) -> Path:
        return self.root / "failures.json"

    def failures(self) -> dict[str, int]:
        if not self.enabled:
            return {}
        try:
            return json.loads(self._fail_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def record_failure(self, key: str) -> None:
        if not self.enabled:
            return
        with _FAIL_LOCK:
            data = self.failures()
            data[key] = data.get(key, 0) + 1
            self._fail_path().write_text(json.dumps(data), encoding="utf-8")

    def keep_raw(self, run_id: str) -> Path:
        """--keep-raw: копия сырых ответов запуска в общем кэше (не рядом с GPKG)."""
        dest = self.root / "raw" / run_id
        dest.mkdir(parents=True, exist_ok=True)
        for k in dict.fromkeys(self.used_keys):
            src = self._path(k)
            if src.exists():
                shutil.copy2(src, dest / src.name)
        return dest
