# Поля выходных слоёв

## Общие поля каждого слоя

| Поле | Тип GPKG | Описание |
|---|---|---|
| `fid` | INTEGER | первичный ключ GeoPackage |
| `geom` | геометрия | EPSG:4326 по умолчанию (`--output-crs`) |
| `osm_type` | TEXT | `node`, `way`, `relation` |
| `osm_id` | INTEGER (64) | ID OSM |
| `osm_uid` | TEXT | `n123`, `w123`, `r123` (устойчивый ключ; удобен в ArcMap, где Integer64 может читаться как Double) |
| `part_no` | INTEGER | номер части после `explode`, иначе 0; `osm_uid + part_no` уникальны |
| `name_ru` | TEXT | строго `name:ru`, иначе NULL |
| `name_local` | TEXT | `name` |
| `name_display` | TEXT | `coalesce(name:ru, name)` для подписей |
| `name_source` | TEXT | `name:ru`, `name` или `missing` |
| `country_code` | TEXT | `RU`, `KZ` или `KZ;RU` (объект в обеих странах). Определяется по граням, на которые линии госграниц делят тайл: страна — та, внутри которой лежит любая часть объекта в пределах AOI; объект, проходящий только по линии границы (река-граница), получает обе страны |
| `source_backend` | TEXT | `overpass` |
| `osm_version` | INTEGER | версия объекта |
| `osm_timestamp` | DATETIME | время версии объекта (UTC) |
| `downloaded_at` | DATETIME | время загрузки (UTC) |
| `recipe_id` | TEXT | ID рецепта |
| `is_clipped` | INTEGER | 1 — геометрия обрезана по AOI |
| `tags_json` | TEXT | все теги объекта в JSON (отключается `--no-tags-json`) |

Имена пользователей OSM, их uid и changeset не сохраняются.

## admin_regions → `admin_regions`

`admin_level` (int), `boundary`, `official_status`, `ref`, `iso3166_2` (тег `ISO3166-2`), `wikidata`, `wikipedia`.

## admin_districts → `admin_districts`

`admin_level` (int), `boundary`, `official_status`, `ref`, `wikidata`, `wikipedia`.

## hydro

Все слои: `waterway`, `water`, `natural`, `landuse`, `intermittent`, `seasonal`, `salt`, `tidal`, `wikidata`.
`seas_point`: `place`, `wikidata`. `seas_polygon`: `place`, `natural`, `water`, `salt`, `wikidata`.
`coastline_line`: `natural`.

| Слой | Правило |
|---|---|
| `rivers_line` | `way[waterway=river]` |
| `seas_polygon` | `place=sea` или `place=ocean` (way/multipolygon); Каспийское море (`natural=water + water=lake + place=sea`) попадает сюда, а не в озёра |
| `rivers_polygon` | `natural=water + water=river`, либо `waterway=riverbank` без `water=*` |
| `reservoirs_polygon` | `natural=water + water=reservoir`, либо `landuse=reservoir` без `water=*` |
| `lakes_polygon` | `natural=water + water=lake`, `oxbow` (старицы), `lagoon` |
| `water_unclassified` | `natural=water` без `water=*` (опционально) |
| `seas_point` | `place=sea` (node/way/relation → точка center) |
| `coastline_line` | `way[natural=coastline]` (опционально) |

## state_borders

`state_border_line`:

| Поле | Описание |
|---|---|
| `countries` | ISO-коды всех государственных relations, в которые входит участок (`CN;KZ`, `KZ;RU`) |
| `border_scope` | `RU-KZ`, `RU-other`, `KZ-other`, `maritime` |
| `maritime` | `yes` для морских участков (`maritime=yes`, `boundary=maritime`, `border_type=territorial/maritime`) |
| `disputed` | значение тега OSM без юридической интерпретации |
| `parent_relations` | `r60189;r214665` |
| `member_role` | роли в relations (`outer`, …) |

`country_outlines` (опционально): `ISO3166-1` → `iso3166_1`, `admin_level`.

Площадные слои взаимоисключающие, приоритет: моря → реки → водохранилища → озёра → без water=*.

## settlements

`settlements_point` (узлы `place=*`) и `settlements_polygon` (замкнутые линии и multipolygon с `place=*`),
`place` ∈ city, town, village, hamlet, isolated_dwelling.

Поля обоих слоёв: `place`, `population` (int), `population_date`, `capital`, `official_status`,
`admin_level` (int), `old_name`, `wikidata`, `wikipedia`.

| Поле | Слой | Значение |
|---|---|---|
| `polygon_uid` | точки | `osm_uid` контура, в котором лежит точка (при нескольких — с тем же названием, затем с тем же `place`, затем меньший) |
| `point_uid` | контуры | `osm_uid` точки этого пункта; для контура без узла — его собственный `osm_uid`, если включено дополнение точек |
| `point_source` | точки | `node` — узел OSM; `polygon` — внутренняя точка контура без узла (параметр `points_from_polygons`, по умолчанию включён) |

## railways

Линии (`railways_line`, `railway_service_line`, `urban_rail_line`, `railway_inactive_line`): `railway`, `usage`
(main, branch, industrial…), `service` (siding, spur, yard, crossover), `gauge`, `electrified`, `voltage` (int),
`frequency`, `tracks` (int), `maxspeed` (int), `highspeed`, `ref`, `operator`, `bridge`, `tunnel`, `layer` (int), `wikidata`.

| Слой | Правило |
|---|---|
| `railways_line` | `railway=rail\|narrow_gauge` без `service=*` |
| `railway_service_line` | `railway=rail\|narrow_gauge` с `service=*` (опционально) |
| `urban_rail_line` | `railway=subway\|light_rail\|tram\|monorail\|funicular` (опционально) |
| `railway_inactive_line` | `railway=abandoned\|disused\|razed\|construction\|proposed\|preserved` (опционально) |
| `railway_stations_point` | `railway=station\|halt`, кроме станций метро, трамвая и т. п.; поля `railway`, `station`, `operator`, `network`, `esr_code` (`esr:user`), `express_code` (`express:user`), `uic_ref`, `wikidata` |

## roads

Поля `roads_main_line`, `roads_local_line`, `roads_track_line`: `highway`, `ref`, `int_ref`, `surface`, `lanes` (int),
`maxspeed` (int), `oneway`, `bridge`, `tunnel`, `layer` (int), `tracktype`, `smoothness`, `access`, `toll`, `wikidata`.
`roads_service_line`: `highway`, `service`, `surface`, `access`, `bridge`, `tunnel`, `layer`.

| Слой | Правило |
|---|---|
| `roads_main_line` | `highway=motorway\|trunk\|primary\|secondary\|tertiary` и их `_link` |
| `roads_local_line` | `highway=unclassified\|residential\|living_street\|road` (опционально) |
| `roads_service_line` | `highway=service` (опционально; в городах объём большой) |
| `roads_track_line` | `highway=track` (опционально) |
