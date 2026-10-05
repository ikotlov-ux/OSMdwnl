"""Интерфейс backend-провайдера."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..errors import ArgumentError


class Provider(ABC):
    name = "base"
    endpoint = ""

    @abstractmethod
    def execute(self, query: str, purpose: str = "") -> dict[str, Any]:
        """Выполняет физический запрос и возвращает Overpass-подобный JSON {'elements': [...]}"""


def get_provider(name: str, config, cache, recipe_tag: str = ""):
    if name == "overpass":
        from .overpass import OverpassProvider
        return OverpassProvider(config.overpass, cache, recipe_tag)
    if name == "pbf":
        from .pbf import PBFProvider
        return PBFProvider()
    if name == "postgis":
        from .postgis import PostGISProvider
        return PostGISProvider()
    raise ArgumentError(f"Неизвестный backend: {name}")
