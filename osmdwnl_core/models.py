"""Базовые модели: bbox, AOI, задачи и план выполнения."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from shapely.geometry import box
from shapely.geometry.base import BaseGeometry


@dataclass(frozen=True)
class BBox:
    """Прямоугольник в EPSG:4326. Внутренний порядок — west, south, east, north."""
    w: float
    s: float
    e: float
    n: float

    def __post_init__(self):
        if not (-180 <= self.w <= 180 and -180 <= self.e <= 180):
            raise ValueError(f"долгота вне диапазона [-180, 180]: {self}")
        if not (-90 <= self.s <= 90 and -90 <= self.n <= 90):
            raise ValueError(f"широта вне диапазона [-90, 90]: {self}")
        if self.s >= self.n:
            raise ValueError(f"south должен быть меньше north: {self}")
        if self.w >= self.e:
            raise ValueError(f"west должен быть меньше east (антимеридиан делится заранее): {self}")

    # --- порядок координат централизован здесь ---
    def wsen(self) -> tuple[float, float, float, float]:
        return (self.w, self.s, self.e, self.n)

    def overpass(self) -> str:
        """Строка bbox для Overpass QL: south,west,north,east."""
        return f"{_fmt(self.s)},{_fmt(self.w)},{_fmt(self.n)},{_fmt(self.e)}"

    @property
    def width(self) -> float:
        return self.e - self.w

    @property
    def height(self) -> float:
        return self.n - self.s

    @property
    def area_deg2(self) -> float:
        return self.width * self.height

    def polygon(self):
        return box(self.w, self.s, self.e, self.n)

    def grid(self, max_area: float) -> list["BBox"]:
        if self.area_deg2 <= max_area:
            return [self]
        import math
        side = math.sqrt(max_area)
        nx = max(1, math.ceil(self.width / side))
        ny = max(1, math.ceil(self.height / side))
        dx, dy = self.width / nx, self.height / ny
        tiles = []
        for j in range(ny):
            for i in range(nx):
                w = self.w + i * dx
                s = self.s + j * dy
                e = self.e if i == nx - 1 else w + dx
                n = self.n if j == ny - 1 else s + dy
                tiles.append(BBox(w, s, e, n))
        return tiles

    def quadrants(self) -> list["BBox"]:
        mx = (self.w + self.e) / 2
        my = (self.s + self.n) / 2
        return [BBox(self.w, self.s, mx, my), BBox(mx, self.s, self.e, my),
                BBox(self.w, my, mx, self.n), BBox(mx, my, self.e, self.n)]

    def label(self) -> str:
        return f"{_fmt(self.w)}_{_fmt(self.s)}_{_fmt(self.e)}_{_fmt(self.n)}"


def _fmt(v: float) -> str:
    s = f"{v:.7f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


@dataclass
class AOIModel:
    source: str                     # путь к файлу или "bbox"
    layer: Optional[str]
    crs: str                        # исходная CRS
    geometry: BaseGeometry          # геометрия обрезки в EPSG:4326 (envelope или точная)
    bboxes: list[BBox]              # 1 или 2 (антимеридиан) bbox запроса
    mode: str                       # envelope | geometry
    name: str                       # имя для шаблона выходного файла
    warnings: list[str] = field(default_factory=list)

    @property
    def envelope_wsen(self) -> tuple[float, float, float, float]:
        if len(self.bboxes) == 1:
            return self.bboxes[0].wsen()
        # антимеридиан: west — западная граница восточного куска
        east_part = max(self.bboxes, key=lambda b: b.w)
        west_part = min(self.bboxes, key=lambda b: b.w)
        return (east_part.w, min(b.s for b in self.bboxes), west_part.e, max(b.n for b in self.bboxes))


@dataclass
class PhysicalQuery:
    purpose: str
    query: str
    tile: Optional[BBox] = None
    depth: int = 0


@dataclass
class ExecutionPlan:
    recipe_id: str
    backend: str
    endpoint: str
    tiles: list[BBox]
    queries: list[PhysicalQuery]
    layers: list[str]
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
