"""PostGIS backend — вторая очередь (раздел 8.4 ТЗ). DSN читается из OSM_DB_DSN."""
from __future__ import annotations

from ..errors import ArgumentError
from .base import Provider


class PostGISProvider(Provider):
    name = "postgis"

    def __init__(self, *_, **__):
        raise ArgumentError(
            "Backend PostGIS не входит в MVP и пока не реализован",
            cause="Требуется адаптер под схему osm2pgsql (flex/classic/custom)",
            action="Используйте --backend overpass")

    def execute(self, query, purpose=""):  # pragma: no cover
        raise NotImplementedError
