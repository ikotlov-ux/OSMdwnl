"""PBF backend — вторая очередь (раздел 8.3 ТЗ). Интерфейс зарезервирован."""
from __future__ import annotations

from ..errors import ArgumentError
from .base import Provider


class PBFProvider(Provider):
    name = "pbf"

    def __init__(self, *_, **__):
        raise ArgumentError(
            "Backend PBF не входит в MVP и пока не реализован",
            cause="Раздел 4.2 ТЗ: PBF через Pyrosm/Pyosmium/Osmium — следующая очередь",
            action="Используйте --backend overpass и уменьшите AOI либо разбейте его на части")

    def execute(self, query, purpose=""):  # pragma: no cover
        raise NotImplementedError
