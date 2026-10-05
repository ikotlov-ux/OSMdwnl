"""Гидрография (раздел 13.2 ТЗ).

Классификация водных полигонов полностью декларативна и задаётся в recipes/hydro.yml:
слои rivers_polygon → reservoirs_polygon → lakes_polygon → water_unclassified входят в одну
exclusive_group, поэтому OSM-объект попадает только в первый подходящий слой. Условие
``water: {exists: false}`` у legacy-клауз обеспечивает приоритет новых тегов water=* над
waterway=riverbank / landuse=reservoir. Дополнительный Python-код для этого не нужен.
"""
from __future__ import annotations

WATER_PRIORITY = ("rivers_polygon", "reservoirs_polygon", "lakes_polygon", "water_unclassified")
