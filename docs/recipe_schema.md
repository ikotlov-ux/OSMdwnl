# Схема рецепта YAML

Источник может иметь `include_enclosing: true` (только для relation): тогда в выборку тайла
попадают и relations, целиком содержащие тайл (через `is_in` центра тайла), — нужно для
крупных единиц (области, районы), граница которых не проходит через тайл.

```yaml
id: hydro                # ASCII, совпадает с именем файла
version: 1                     # увеличивайте при изменении логики (входит в ключ кэша)
label_ru: "…"                  # подпись (только в метаданных)
type: standard                 # standard | raw_overpass (нужен --allow-raw-query)
countries: [RU, KZ]            # ISO 3166-1 alpha-2; заменяются полем «Страны» / --countries
country_profiles:              # значения для подстановки "$profile:<ключ>"
  default: {district_admin_levels: [6]}   # для стран без своего профиля
  RU: {district_admin_levels: [6]}
output_name: "{recipe}__{aoi}__{date}.gpkg"
clip_geometry: true            # обрезка по умолчанию (CLI --clip/--no-clip важнее)
require_name_ru: false
keep_tags_json: true           # иначе берётся из config.yml
language: {preferred: ["name:ru"], fallback: ["name"]}
params: {mode: all_target_borders}     # переопределяются через --param key=value
param_choices: {mode: [all_target_borders, shared_only]}   # выпадающий список в окне
param_labels_ru: {mode: "Режим границы"}                        # подписи параметров в окне
tiling: {max_bbox_area_deg2: 4.0}
warnings_ru: ["…"]             # выводятся при запуске и пишутся в метаданные
sources: [...]
outputs: [...]
checks: [...]
links:                         # связь точечного и площадного слоёв (поля polygon_uid / point_uid)
  - {points: settlements_point, polygons: settlements_polygon, fill_points: points_from_polygons}
```

## sources

| Поле | Значения |
|---|---|
| `id` | имя источника |
| `kind` | `tags` (по умолчанию), `country_border_members`, `raw_overpass` |
| `geometry` | список из `node`, `way`, `relation`, `nwr` |
| `filters` | список OR-клауз; клауза — словарь AND-условий |
| `country_scope` | `none` — только bbox; `area` — отдельно внутри каждой страны из `countries`, в результате заполняется `country_code` |
| `country_resolution` | `membership` (список стран) или `point` (объекты на стыке стран уточняются через `is_in` по внутренней точке) |
| `geometry_output` | `full` (`out meta geom`; relations грузятся вторым этапом), `center` (`out center`), `tags` (только теги, для проверок) |
| `query` | только для `raw_overpass`; `{{bbox}}` заменяется на `S,W,N,E` |

### Условия фильтра

| Запись | Смысл | Overpass QL |
|---|---|---|
| `waterway: river` | точное значение | `["waterway"="river"]` |
| `water: [lake, reservoir]` | одно из значений | `["water"~"^(lake\|reservoir)$"]` |
| `name: "*"` или `name: true` | тег существует | `["name"]` |
| `water: {exists: false}` | тега нет (NOT) | `[!"water"]` |
| `a: {not: b}` / `a: {not_in: [b, c]}` | не равно | `["a"!="b"]` |
| `ref: {regex: "^M-"}` | регулярное выражение | `["ref"~"^M-"]` |
| `admin_level: "$profile:district_admin_levels"` | значения из профиля страны | — |

Ключи проверяются по шаблону `[A-Za-z0-9_:.-]+`, а значения не могут содержать кавычки и переводы строк.
Пустой фильтр запрещён.

## outputs

| Поле | Значения |
|---|---|
| `layer` | ASCII snake_case — имя слоя GPKG |
| `from` | список источников |
| `geometry_type` | `Point`, `MultiPoint`, `LineString`, `MultiLineString`, `Polygon`, `MultiPolygon` |
| `where` | дополнительный фильтр по тегам на стороне Python (тот же синтаксис, без `$profile`) |
| `exclusive_group` | объект попадает только в первый по порядку слой группы |
| `operations` | `deduplicate`, `make_valid`, `clip`, `explode`, `polygon_to_boundary`, `filter_geometry`, `{op: dissolve, by: [...]}`, `{op: sort, by: [...]}`, `{op: rename_fields, map: {...}}`. `spatial_join` и `derive_fields` зарезервированы |
| `fields` | имена тегов или `{name, tag, type: text\|int\|real}` |
| `transform` | `state_borders`, `country_outlines` |
| `enabled` / `enabled_if` | слой по умолчанию выключен либо включается параметром; `--enable-layer`/`--disable-layer` |
| `clip_geometry` | переопределение на уровне слоя |

Порядок операций фиксирован (раздел 10.2 ТЗ): сборка → 2D → удаление пустых → make_valid → извлечение
типа → дедупликация → clip → explode → multi → dissolve → sort → rename. Упрощение геометрии и слияние по имени
выполняются только явно заданными операциями.

## checks

```yaml
checks:
  - type: orphan_members        # ways из source, не входящие ни в одно отношение из relations
    source: district_ways_tagged
    relations: district_relations
    code: W_ORPHAN_DISTRICT_WAY
```

### Поле с переводом значения (с 1.8.0)

```yaml
fields:
  - name: wetland_type_ru
    tag: "wetland|natural"      # значение первого присутствующего тега
    values_ru: {bog: "верховое болото", fen: "низинное болото"}   # прочие значения — как есть
    default_ru: "болото (тип не указан)"                          # если ни одного тега нет
```
