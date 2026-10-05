"""Конфигурация приложения (раздел 17 ТЗ). Проверяется Pydantic."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from .errors import ArgumentError

APP_DIR = Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "OSMdwnl"
    return Path.home() / ".cache" / "osmdwnl"


class OverpassConfig(BaseModel):
    endpoints: list[str] = Field(default_factory=lambda: [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    ])
    timeout_seconds: int = 180
    maxsize_bytes: int = 536870912            # relations целиком, полигоны стран
    tile_timeout_seconds: int = 120           # тайлы, линии границ, is_in — «лёгкий» заголовок
    tile_maxsize_bytes: int = 134217728       # 128 МБ: такие запросы сервер принимает охотнее
    split_after_busy: int = 6                 # тайл, которому отказали N раз подряд, делится на 4
    connect_timeout_seconds: int = 30
    retries: int = 8
    endpoint_cooldown_seconds: float = 900   # зеркало без соединения не используется 15 мин
    slow_endpoint_cooldown_seconds: float = 120   # медленное/оборвавшее ответ зеркало — 2 мин
    busy_retries: int = 20   # доп. повторы при «server too busy»/429 (пауза 10, 20, 30… с)
    switch_after_errors: int = 2
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 120.0
    concurrency: int = 2                      # параллельные запросы — только к разным зеркалам
    check_status: bool = True
    status_max_wait_seconds: int = 180
    relation_batch_size: int = 100
    min_pause_seconds: float = 1.0
    smart_country_filter: bool = True   # тайлы без госграницы — запрос по bbox вместо areas стран
    user_agent: str = "OSMdwnl/1.0 (+set overpass.user_agent with your contact in config.yml)"

    @field_validator("concurrency")
    @classmethod
    def _one(cls, v: int) -> int:
        if not 1 <= v <= 3:
            raise ValueError("concurrency: от 1 до 3 (каждый поток работает со своим зеркалом)")
        return v


class TilingConfig(BaseModel):
    enabled: bool = True
    max_bbox_area_deg2: float = 1.0
    max_split_depth: int = 6


class CacheConfig(BaseModel):
    enabled: bool = True
    ttl_days: float = 30
    dir: Optional[str] = None


class OutputConfig(BaseModel):
    crs: str = "EPSG:4326"
    spatial_index: bool = True
    keep_tags_json: bool = True
    overwrite: bool = False
    max_warning_rows: int = 5000
    store_aoi_wkt_max_chars: int = 200000


class PBFConfig(BaseModel):
    index_url: str = "https://download.geofabrik.de/index-v1.json"
    keep_files: bool = False                  # True — не удалять скачанные PBF после обработки
    download_speed_mb_s: float = 5.0          # для оценки времени в окне выбора источника
    process_speed_mb_s: float = 3.0


class PlannerConfig(BaseModel):
    recommend_pbf_area_deg2: float = 25.0
    overpass_hard_limit_deg2: float = 400.0


class AppConfig(BaseModel):
    overpass: OverpassConfig = Field(default_factory=OverpassConfig)
    tiling: TilingConfig = Field(default_factory=TilingConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    pbf: PBFConfig = Field(default_factory=PBFConfig)
    recipe_dirs: list[str] = Field(default_factory=list)
    log_dir: Optional[str] = None

    def cache_dir(self) -> Path:
        return Path(self.cache.dir) if self.cache.dir else default_data_dir() / "cache"

    def logs_dir(self) -> Path:
        return Path(self.log_dir) if self.log_dir else default_data_dir() / "logs"


def load_config(path: str | None) -> AppConfig:
    candidates = [Path(path)] if path else [APP_DIR / "config.yml"]
    for p in candidates:
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                return AppConfig.model_validate(data)
            except (yaml.YAMLError, ValidationError) as exc:
                raise ArgumentError(f"Ошибка в файле конфигурации {p}", cause=str(exc),
                                    action="Исправьте config.yml по образцу из README") from exc
        elif path:
            raise ArgumentError(f"Файл конфигурации не найден: {p}")
    return AppConfig()
