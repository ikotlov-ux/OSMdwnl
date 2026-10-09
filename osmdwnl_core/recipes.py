"""Декларативные рецепты YAML (раздел 9 ТЗ).

Фильтр тегов — список OR-клауз; клауза — словарь AND-условий ``ключ: условие``.
Условие:
  "value"                 точное значение
  [a, b]                  одно из значений
  "*"  или  true          тег существует
  {exists: false}         тега нет           (NOT)
  {not: v} / {not_in: []} значение не равно  (NOT)
  {regex: "^..$"}         регулярное выражение
  {in: [..]}              одно из значений
  "$profile:<key>"        значения из country_profiles для текущей страны
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import RecipeError

KEY_RE = re.compile(r"^[A-Za-z0-9_:\-.]+$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*$")
BAD_VALUE_RE = re.compile(r'["\\\n\r\t]')
GEOM_TYPES = ("Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon")
OPERATIONS = {"deduplicate", "make_valid", "clip", "explode", "dissolve", "polygon_to_boundary",
              "filter_geometry", "rename_fields", "sort", "spatial_join", "derive_fields", "merge"}
NOT_IN_MVP = {"spatial_join", "derive_fields"}
TRANSFORMS = {"state_borders", "country_outlines"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FieldSpec(StrictModel):
    name: str
    tag: Optional[str] = None            # "a|b" — значение первого присутствующего тега
    type: Literal["text", "int", "real"] = "text"
    values_ru: Optional[dict[str, str]] = None   # перевод значения тега (прочие — как есть)
    default_ru: Optional[str] = None             # если ни одного тега нет

    @field_validator("name")
    @classmethod
    def _ascii(cls, v):
        if not FIELD_RE.match(v):
            raise ValueError(f"имя поля должно быть ASCII snake_case: {v}")
        return v


class SourceSpec(StrictModel):
    id: str
    kind: Literal["tags", "country_border_members", "raw_overpass"] = "tags"
    geometry: list[Literal["node", "way", "relation", "nwr"]] = Field(default_factory=lambda: ["way"])
    filters: list[dict[str, Any]] = Field(default_factory=list)
    country_scope: Literal["none", "area"] = "none"
    country_resolution: Literal["membership", "point"] = "membership"
    geometry_output: Literal["full", "center", "tags"] = "full"
    query: Optional[str] = None          # только kind=raw_overpass
    include_enclosing: bool = False      # + relations, целиком содержащие тайл (is_in центра тайла)
    label_ru: Optional[str] = None

    def element_types(self) -> list[str]:
        out: list[str] = []
        for g in self.geometry:
            for t in (["node", "way", "relation"] if g == "nwr" else [g]):
                if t not in out:
                    out.append(t)
        return out


class OutputSpec(StrictModel):
    layer: str
    from_: list[str] = Field(alias="from")
    geometry_type: Literal[GEOM_TYPES]  # type: ignore[valid-type]
    label_ru: Optional[str] = None
    where: Optional[list[dict[str, Any]]] = None
    exclusive_group: Optional[str] = None
    operations: list[Union[str, dict[str, Any]]] = Field(
        default_factory=lambda: ["deduplicate", "make_valid", "clip"])
    fields: list[Union[str, FieldSpec]] = Field(default_factory=list)
    transform: Optional[str] = None
    enabled: bool = True
    enabled_if: Optional[str] = None
    clip_geometry: Optional[bool] = None

    @field_validator("layer")
    @classmethod
    def _layer_ascii(cls, v):
        if not FIELD_RE.match(v):
            raise ValueError(f"имя слоя должно быть ASCII snake_case: {v}")
        return v

    def field_specs(self) -> list[FieldSpec]:
        out = []
        for f in self.fields:
            if isinstance(f, str):
                out.append(FieldSpec(name=re.sub(r"[^a-z0-9_]", "_", f.lower()), tag=f))
            else:
                out.append(f)
        return out

    def op_names(self) -> list[str]:
        return [o if isinstance(o, str) else o.get("op", "") for o in self.operations]


class CheckSpec(StrictModel):
    type: Literal["orphan_members"]
    source: str
    relations: str
    code: str = "W_ORPHAN_WAY"
    message_ru: str = "объект с тегами границы не входит ни в одно отношение"


class LinkSpec(StrictModel):
    """Связь точечного и площадного слоёв (населённые пункты: узел place=* ↔ контур)."""
    points: str
    polygons: str
    fill_points: Union[bool, str] = False   # bool или имя bool-параметра рецепта


class LanguageSpec(StrictModel):
    preferred: list[str] = Field(default_factory=lambda: ["name:ru"])
    fallback: list[str] = Field(default_factory=lambda: ["name"])


class TilingOverride(StrictModel):
    max_bbox_area_deg2: Optional[float] = None
    enabled: Optional[bool] = None


class Recipe(StrictModel):
    id: str
    version: int
    label_ru: str
    description_ru: Optional[str] = None
    type: Literal["standard", "raw_overpass"] = "standard"
    countries: list[str] = Field(default_factory=list)
    country_profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    output_name: str = "{recipe}__{aoi}__{date}.gpkg"
    clip_geometry: bool = True
    require_name_ru: bool = False
    keep_tags_json: Optional[bool] = None
    language: LanguageSpec = Field(default_factory=LanguageSpec)
    params: dict[str, Any] = Field(default_factory=dict)
    param_choices: dict[str, list[str]] = Field(default_factory=dict)   # варианты для GUI
    param_labels_ru: dict[str, str] = Field(default_factory=dict)
    tiling: TilingOverride = Field(default_factory=TilingOverride)
    sources: list[SourceSpec]
    outputs: list[OutputSpec]
    checks: list[CheckSpec] = Field(default_factory=list)
    links: list[LinkSpec] = Field(default_factory=list)
    warnings_ru: list[str] = Field(default_factory=list)
    path: Optional[str] = Field(default=None, exclude=True)
    sha256: Optional[str] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _check(self):
        src_ids = [s.id for s in self.sources]
        if len(src_ids) != len(set(src_ids)):
            raise ValueError("повторяющиеся id источников")
        layers = [o.layer for o in self.outputs]
        if len(layers) != len(set(layers)):
            raise ValueError("повторяющиеся имена выходных слоёв")
        for c in self.countries:
            if not re.fullmatch(r"[A-Z]{2}", c):
                raise ValueError(f"код страны должен быть ISO 3166-1 alpha-2: {c}")
        for s in self.sources:
            if s.kind == "raw_overpass" and self.type != "raw_overpass":
                raise ValueError("источник raw_overpass допустим только в рецепте type: raw_overpass")
            if s.kind == "raw_overpass" and not s.query:
                raise ValueError(f"источник {s.id}: нужен query")
            if s.country_scope == "area" and not self.countries:
                raise ValueError(f"источник {s.id}: country_scope=area требует список countries")
            for clause in s.filters:
                validate_clause(clause, self, s.id)
            if s.kind == "tags" and not s.filters:
                raise ValueError(f"источник {s.id}: пустой фильтр запрещён (выгрузил бы все объекты)")
        for o in self.outputs:
            for sid in o.from_:
                if sid not in src_ids:
                    raise ValueError(f"слой {o.layer}: неизвестный источник {sid}")
            for clause in o.where or []:
                validate_clause(clause, self, o.layer, allow_profile=False)
            for name in o.op_names():
                if name not in OPERATIONS:
                    raise ValueError(f"слой {o.layer}: неизвестная операция {name}")
                if name in NOT_IN_MVP:
                    raise ValueError(f"слой {o.layer}: операция {name} зарезервирована и не реализована в MVP")
            if o.transform and o.transform not in TRANSFORMS:
                raise ValueError(f"слой {o.layer}: неизвестное преобразование {o.transform}")
        for ln in self.links:
            for lyr in (ln.points, ln.polygons):
                if lyr not in layers:
                    raise ValueError(f"links: неизвестный слой {lyr}")
            if isinstance(ln.fill_points, str) and ln.fill_points not in self.params:
                raise ValueError(f"links: параметр {ln.fill_points} не объявлен в params")
            if o.enabled_if and o.enabled_if not in self.params:
                raise ValueError(f"слой {o.layer}: enabled_if ссылается на неизвестный параметр")
        for ch in self.checks:
            if ch.source not in src_ids or ch.relations not in src_ids:
                raise ValueError(f"проверка {ch.type}: неизвестный источник")
        return self

    # --- helpers ---
    def active_outputs(self) -> list[OutputSpec]:
        out = []
        for o in self.outputs:
            if not o.enabled:
                continue
            if o.enabled_if and not _truthy(self.params.get(o.enabled_if)):
                continue
            out.append(o)
        return out

    def active_sources(self) -> list[SourceSpec]:
        need = {sid for o in self.active_outputs() for sid in o.from_}
        need |= {c.source for c in self.checks} | {c.relations for c in self.checks}
        return [s for s in self.sources if s.id in need]

    def source(self, sid: str) -> SourceSpec:
        return next(s for s in self.sources if s.id == sid)

    def profile_values(self, country: str, key: str) -> list[str]:
        prof = self.country_profiles.get(country, {})
        if key not in prof:
            prof = self.country_profiles.get("default", {})   # общий профиль для прочих стран
        if key not in prof:
            raise RecipeError(f"country_profiles.{country}.{key} не задан в рецепте {self.id}",
                              action="Добавьте профиль страны или раздел default в рецепт")
        vals = prof[key]
        return [str(v) for v in (vals if isinstance(vals, list) else [vals])]


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "да")
    return bool(v)


def validate_clause(clause: dict, recipe: Recipe, where: str, allow_profile: bool = True) -> None:
    if not isinstance(clause, dict) or not clause:
        raise ValueError(f"{where}: клауза фильтра должна быть непустым словарём")
    for key, cond in clause.items():
        if not KEY_RE.match(str(key)):
            raise ValueError(f"{where}: недопустимый ключ тега '{key}'")
        _validate_cond(cond, f"{where}.{key}", allow_profile)


def _validate_cond(cond: Any, where: str, allow_profile: bool) -> None:
    if cond is True or cond == "*":
        return
    if isinstance(cond, (int, float)) and not isinstance(cond, bool):
        cond = str(cond)
    if isinstance(cond, str):
        if cond.startswith("$profile:"):
            if not allow_profile:
                raise ValueError(f"{where}: $profile допустим только в фильтрах источников")
            return
        if BAD_VALUE_RE.search(cond):
            raise ValueError(f"{where}: недопустимые символы в значении")
        return
    if isinstance(cond, list):
        if not cond:
            raise ValueError(f"{where}: пустой список значений")
        for v in cond:
            _validate_cond(str(v), where, allow_profile)
        return
    if isinstance(cond, dict):
        if len(cond) != 1:
            raise ValueError(f"{where}: в условии-словаре допускается ровно один оператор")
        op, val = next(iter(cond.items()))
        if op == "exists":
            if not isinstance(val, bool):
                raise ValueError(f"{where}: exists должен быть true/false")
        elif op in ("not", "in", "not_in"):
            for v in (val if isinstance(val, list) else [val]):
                _validate_cond(str(v), where, allow_profile)
        elif op == "regex":
            if not isinstance(val, str) or BAD_VALUE_RE.search(val.replace("\\", "")):
                raise ValueError(f"{where}: некорректный regex")
            try:
                re.compile(val)
            except re.error as exc:
                raise ValueError(f"{where}: regex не компилируется: {exc}") from exc
        else:
            raise ValueError(f"{where}: неизвестный оператор {op}")
        return
    raise ValueError(f"{where}: неподдерживаемое условие {cond!r}")


# ---------------- Overpass QL и Python-проверка тех же условий ----------------
_ERE_SPECIAL = re.compile(r"([.\[\](){}*+?|^$\\])")


def ere_escape(v: str) -> str:
    return _ERE_SPECIAL.sub(r"\\\1", v)


def ql_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _resolve(cond: Any, recipe: Recipe | None, country: str | None) -> Any:
    if isinstance(cond, str) and cond.startswith("$profile:"):
        if recipe is None or country is None:
            raise RecipeError("$profile используется без страны (нужен country_scope: area)")
        return recipe.profile_values(country, cond.split(":", 1)[1])
    return cond


def cond_to_ql(key: str, cond: Any, recipe: Recipe | None = None, country: str | None = None) -> str:
    k = ql_str(key)
    cond = _resolve(cond, recipe, country)
    if cond is True or cond == "*":
        return f"[{k}]"
    if isinstance(cond, (int, float)) and not isinstance(cond, bool):
        cond = str(cond)
    if isinstance(cond, str):
        return f"[{k}={ql_str(cond)}]"
    if isinstance(cond, list):
        vals = [str(v) for v in cond]
        if len(vals) == 1:
            return f"[{k}={ql_str(vals[0])}]"
        return f"[{k}~{ql_str('^(' + '|'.join(ere_escape(v) for v in vals) + ')$')}]"
    op, val = next(iter(cond.items()))
    if op == "exists":
        return f"[{k}]" if val else f"[!{k}]"
    if op == "not":
        return f"[{k}!={ql_str(str(val))}]"
    if op == "in":
        return cond_to_ql(key, list(val) if isinstance(val, list) else [val])
    if op == "not_in":
        vals = [str(v) for v in (val if isinstance(val, list) else [val])]
        return f"[{k}!~{ql_str('^(' + '|'.join(ere_escape(v) for v in vals) + ')$')}]"
    if op == "regex":
        return f"[{k}~{ql_str(val)}]"
    raise RecipeError(f"неизвестный оператор {op}")


def clause_to_ql(clause: dict, recipe: Recipe | None = None, country: str | None = None) -> str:
    # условия существования ставим первыми — Overpass быстрее фильтрует по ключу
    items = sorted(clause.items(), key=lambda kv: isinstance(kv[1], dict) and "exists" in kv[1]
                   and kv[1]["exists"] is False)
    return "".join(cond_to_ql(k, c, recipe, country) for k, c in items)


def match_cond(tags: dict, key: str, cond: Any) -> bool:
    v = tags.get(key)
    if cond is True or cond == "*":
        return v is not None
    if isinstance(cond, (int, float)) and not isinstance(cond, bool):
        cond = str(cond)
    if isinstance(cond, str):
        return v == cond
    if isinstance(cond, list):
        return v in [str(x) for x in cond]
    op, val = next(iter(cond.items()))
    if op == "exists":
        return (v is not None) == bool(val)
    if op == "not":
        return v != str(val)
    if op == "in":
        return v in [str(x) for x in (val if isinstance(val, list) else [val])]
    if op == "not_in":
        return v not in [str(x) for x in (val if isinstance(val, list) else [val])]
    if op == "regex":
        return v is not None and re.search(val, v) is not None
    return False


def match_any(tags: dict, clauses: list[dict] | None) -> bool:
    if not clauses:
        return True
    return any(all(match_cond(tags, k, c) for k, c in cl.items()) for cl in clauses)


# ---------------- загрузка ----------------
def recipe_dirs(extra: list[str] | None = None) -> list[Path]:
    from .config import APP_DIR
    dirs = [Path(d) for d in (extra or [])] + [APP_DIR / "recipes"]
    return [d for d in dirs if d.is_dir()]


def list_recipes(extra_dirs: list[str] | None = None) -> list[Recipe]:
    out, seen = [], set()
    for d in recipe_dirs(extra_dirs):
        for p in sorted(d.glob("*.y*ml")):
            if p.stem in LEGACY_IDS:      # файл прежней версии, оставшийся после распаковки поверх
                continue
            try:
                r = load_recipe(str(p))
            except RecipeError:
                continue
            if r.id not in seen:
                seen.add(r.id)
                out.append(r)
    return out


LEGACY_IDS = {"admin_districts_ru_kz": "admin_districts", "hydro_ru_kz": "hydro",
              "state_borders_ru_kz": "state_borders"}


def load_recipe(id_or_path: str, extra_dirs: list[str] | None = None, allow_raw: bool = False) -> Recipe:
    id_or_path = LEGACY_IDS.get(id_or_path, id_or_path)   # прежние ID (1.0–1.2) продолжают работать
    p = Path(id_or_path)
    if not (p.suffix.lower() in (".yml", ".yaml") and p.exists()):
        found = None
        for d in recipe_dirs(extra_dirs):
            for cand in (d / f"{id_or_path}.yml", d / f"{id_or_path}.yaml"):
                if cand.exists():
                    found = cand
                    break
            if found:
                break
        if not found:
            raise RecipeError(f"Рецепт не найден: {id_or_path}", action="Посмотрите список: --list-recipes")
        p = found
    text = p.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)  # safe_load: без выполнения кода
        recipe = Recipe.model_validate(data)
    except yaml.YAMLError as exc:
        raise RecipeError(f"Рецепт {p.name}: ошибка YAML", cause=str(exc)) from exc
    except ValidationError as exc:
        raise RecipeError(f"Рецепт {p.name}: неверная схема", cause=_short_validation(exc)) from exc
    if recipe.type == "raw_overpass" and not allow_raw:
        raise RecipeError("Рецепт с сырым Overpass QL требует флаг --allow-raw-query")
    recipe.path = str(p)
    recipe.sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return recipe


def _short_validation(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:8])


def apply_overrides(recipe: Recipe, params: dict[str, str], enable: list[str], disable: list[str],
                    countries: list[str] | None = None) -> Recipe:
    if countries:
        cc = [c.strip().upper() for c in countries if c.strip()]
        bad = [c for c in cc if not re.fullmatch(r"[A-Z]{2}", c)]
        if bad:
            raise RecipeError(f"Неверный код страны: {', '.join(bad)}",
                              action="Используйте ISO 3166-1 alpha-2, например RU,KZ,BY")
        recipe.countries = list(dict.fromkeys(cc))
        for s in recipe.sources:                    # профили ($profile) должны быть для каждой страны
            for clause in s.filters:
                for c in recipe.countries:
                    clause_to_ql(clause, recipe, c)
    for k, v in params.items():
        if k not in recipe.params:
            raise RecipeError(f"Параметр '{k}' не объявлен в рецепте {recipe.id}",
                              action=f"Доступно: {', '.join(recipe.params) or 'нет параметров'}")
        default = recipe.params[k]
        if isinstance(default, bool):
            recipe.params[k] = _truthy(v)
        elif isinstance(default, int):
            recipe.params[k] = int(v)
        else:
            recipe.params[k] = v
    names = {o.layer for o in recipe.outputs}
    for lyr in enable + disable:
        if lyr not in names:
            raise RecipeError(f"Слой '{lyr}' отсутствует в рецепте {recipe.id}")
    for o in recipe.outputs:
        if o.layer in enable:
            o.enabled = True
            if o.enabled_if:
                recipe.params[o.enabled_if] = True
        if o.layer in disable:
            o.enabled = False
    return recipe


def recipe_fingerprint(recipe: Recipe) -> str:
    payload = json.dumps({"id": recipe.id, "v": recipe.version, "sha": recipe.sha256,
                          "params": recipe.params, "countries": recipe.countries}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
