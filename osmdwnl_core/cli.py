"""Командная строка и интерактивный режим OSMdwnl (раздел 6 ТЗ)."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import platform
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from . import ATTRIBUTION, LICENSE, SOFTWARE_NAME, __version__
from .aoi import aoi_from_bbox, read_aoi
from .cache import ResponseCache, query_hash
from .config import AppConfig, load_config
from .errors import ArgumentError, OSMDwnlError, QualityError
from .models import AOIModel
from .planner import Executor, QueryPlanner, choose_backend
from .providers.base import get_provider
from .recipes import Recipe, apply_overrides, list_recipes, load_recipe, recipe_fingerprint
from .transforms.boundaries import (STATE_BORDER_FIELDS, country_outline_candidates,
                                    state_border_candidates)
from .transforms.common import STAT_COLUMNS, BuildContext, build_layer, now_utc
from .transforms.links import apply_links
from .validation import validate_gpkg
from .writers.gpkg import GeoPackageWriter, check_target

log = logging.getLogger("osmdwnl")
OSM_DISCLAIMER_BORDERS = ("Данные OSM не являются официальным юридическим источником "
                          "прохождения государственной границы.")


# ------------------------------------------------------------------ args
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="OSMdwnl.py",
        description="Загрузка тематических слоёв OpenStreetMap для AOI: один запрос — один GeoPackage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Коды возврата: 0 успех; 2 аргументы/AOI; 3 рецепт; 4 сеть/API; 5 парсинг/геометрия; "
                "6 запись/блокировка GPKG; 7 контроль качества.\n" + ATTRIBUTION + ", " + LICENSE))
    a = p.add_argument_group("AOI")
    a.add_argument("--aoi", metavar="PATH", help="векторный файл AOI (GPKG, SHP, GeoJSON, KML, GDB, FGB…)")
    a.add_argument("--aoi-layer", metavar="NAME", help="слой внутри контейнера (GPKG/GDB)")
    a.add_argument("--aoi-crs", metavar="EPSG:XXXX", help="CRS AOI, если она не задана в файле")
    a.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"),
                   help="прямоугольник в EPSG:4326, порядок west south east north")
    a.add_argument("--aoi-mode", choices=["envelope", "geometry"], default="envelope",
                   help="обрезка по экстенту (по умолчанию) или по точной геометрии AOI")
    r = p.add_argument_group("Рецепт")
    r.add_argument("--recipe", metavar="ID|PATH", help="ID рецепта или путь к YAML")
    r.add_argument("--countries", metavar="CC,CC",
                   help="страны (ISO 3166-1 alpha-2 через запятую) вместо заданных в рецепте, напр. RU,KZ,BY")
    r.add_argument("--list-recipes", action="store_true", help="показать доступные рецепты")
    r.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                   help="переопределить параметр рецепта, напр. --param mode=shared_only")
    r.add_argument("--enable-layer", action="append", default=[], metavar="LAYER")
    r.add_argument("--disable-layer", action="append", default=[], metavar="LAYER")
    r.add_argument("--allow-raw-query", action="store_true", help="разрешить рецепты type: raw_overpass")
    o = p.add_argument_group("Вывод")
    o.add_argument("--output", metavar="DIR|FILE.gpkg", help="папка или имя .gpkg")
    o.add_argument("--output-crs", metavar="EPSG:XXXX", help="CRS результата (по умолчанию EPSG:4326)")
    c = o.add_mutually_exclusive_group()
    c.add_argument("--clip", dest="clip", action="store_true", default=None, help="обрезать геометрию по AOI")
    c.add_argument("--no-clip", dest="clip", action="store_false", help="сохранять объекты целиком")
    o.add_argument("--overwrite", action="store_true", help="заменить существующий GeoPackage целиком")
    o.add_argument("--fail-on-empty", action="store_true", help="код 7, если все слои пусты")
    o.add_argument("--no-tags-json", action="store_true", help="не сохранять поле tags_json")
    s = p.add_argument_group("Источник и сеть")
    s.add_argument("--backend", choices=["auto", "overpass", "pbf", "postgis"], default="auto")
    s.add_argument("--endpoint", metavar="URL", help="использовать только это зеркало Overpass")
    s.add_argument("--cache-dir", metavar="PATH")
    s.add_argument("--no-cache", action="store_true", help="не читать и не писать кэш")
    s.add_argument("--keep-raw", action="store_true", help="скопировать сырые ответы запуска в кэш/raw/<run_id>")
    s.add_argument("--force", action="store_true", help="разрешить очень большой AOI для Overpass")
    s.add_argument("--keep-pbf", action="store_true",
                   help="PBF: не удалять скачанные выгрузки Geofabrik после обработки")
    s.add_argument("--pbf-batch", metavar="JSON", help=argparse.SUPPRESS)   # пакет заданий из окна
    g = p.add_argument_group("Общее")
    g.add_argument("--gui", action="store_true",
                   help="открыть окно выбора файлов (по умолчанию при запуске без параметров)")
    g.add_argument("--console", action="store_true", help="интерактивный режим в консоли вместо окна")
    g.add_argument("--config", metavar="PATH", help="config.yml")
    g.add_argument("--dry-run", action="store_true", help="показать план и запросы без обращения к сети")
    g.add_argument("--yes", "-y", action="store_true", help="не спрашивать подтверждение плана")
    g.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    g.add_argument("--version", action="version", version=f"{SOFTWARE_NAME} {__version__}")
    return p


# ------------------------------------------------------------------ logging
class _Mask(logging.Filter):
    def filter(self, record):
        msg = str(record.getMessage())
        import re
        msg2 = re.sub(r"(postgres(?:ql)?://[^:]+:)[^@]+@", r"\1***@", msg)
        msg2 = re.sub(r"(password|token|dsn)=\S+", r"\1=***", msg2, flags=re.I)
        if msg2 != msg:
            record.msg, record.args = msg2, ()
        return True


def setup_logging(level: str, log_dir: Path, run_id: str) -> Path:
    log.setLevel(logging.DEBUG)
    for h in list(log.handlers):
        h.close()
    log.handlers.clear()
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(getattr(logging, level))
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    ch.addFilter(_Mask())
    log.addHandler(ch)
    log_file = log_dir / f"OSMdwnl_{date.today():%Y%m%d}.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(f"%(asctime)s {run_id[:8]} %(levelname)s %(message)s"))
        fh.addFilter(_Mask())
        log.addHandler(fh)
    except OSError:
        log_file = None
    return log_file


# ------------------------------------------------------------------ interactive
def _ask(prompt: str, default: Optional[str] = None) -> str:
    sfx = f" [{default}]" if default else ""
    val = input(f"{prompt}{sfx}: ").strip().strip('"')
    return val or (default or "")


def _choose(prompt: str, options: list[str]) -> str:
    for i, o in enumerate(options, 1):
        print(f"  {i}. {o}")
    while True:
        v = _ask(prompt, "1")
        if v.isdigit() and 1 <= int(v) <= len(options):
            return options[int(v) - 1]
        if v in options:
            return v
        print("  Неверный выбор")


def _pick_file() -> Optional[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        path = filedialog.askopenfilename(title="AOI", filetypes=[
            ("Векторные данные", "*.gpkg *.shp *.geojson *.json *.kml *.fgb"), ("Все файлы", "*.*")])
        root.destroy()
        return path or None
    except Exception:
        return None


def interactive(args, config: AppConfig):
    print(f"{SOFTWARE_NAME} {__version__} — интерактивный режим (Ctrl+C — выход)\n")
    if not args.aoi and not args.bbox:
        v = _ask("1) Путь к AOI, 'bbox' для ввода координат или пусто — диалог выбора файла")
        if v.lower() == "bbox":
            nums = _ask("   west south east north (EPSG:4326)").replace(",", " ").split()
            args.bbox = [float(x) for x in nums]
        else:
            args.aoi = v or _pick_file()
            if not args.aoi:
                raise ArgumentError("AOI не выбран")
    if not args.recipe:
        recs = list_recipes(config.recipe_dirs)
        print("3) Рецепт:")
        lab = [f"{r.id} — {r.label_ru}" for r in recs]
        args.recipe = recs[lab.index(_choose("   номер", lab))].id
    if args.aoi:
        print("4) Режим обрезки:")
        args.aoi_mode = _choose("   номер", ["envelope", "geometry"])
    if not args.output:
        args.output = _ask("5) Папка результата", str(Path.cwd()))
    return args


# ------------------------------------------------------------------ helpers
def _params(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items:
        if "=" not in it:
            raise ArgumentError(f"--param ожидает KEY=VALUE: {it}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def output_path(args, recipe: Recipe, aoi: AOIModel) -> Path:
    out = Path(args.output or ".")
    if out.suffix.lower() == ".gpkg":
        return out
    name = recipe.output_name.format(recipe=recipe.id, aoi=aoi.name, date=f"{date.today():%Y%m%d}")
    if not name.lower().endswith(".gpkg"):
        name += ".gpkg"
    return out / name


def print_plan(plan, aoi: AOIModel, recipe: Recipe, target: Path, show_queries: bool) -> None:
    w, s, e, n = aoi.envelope_wsen
    print(f"\nРецепт:   {recipe.id} v{recipe.version} — {recipe.label_ru}")
    print(f"AOI:      {aoi.source}{' / ' + aoi.layer if aoi.layer else ''}  (CRS {aoi.crs}, режим {aoi.mode})")
    print(f"BBox WSEN: {w:.6f} {s:.6f} {e:.6f} {n:.6f}")
    for b in aoi.bboxes:
        print(f"BBox Overpass (S,W,N,E): {b.overpass()}  площадь {b.area_deg2:.3f} град²")
    print(f"Backend:  {plan.backend}  {plan.endpoint}")
    print(f"Тайлов:   {len(plan.tiles)}")
    print(f"Слои:     {', '.join(plan.layers)}")
    print(f"Результат: {target}")
    for nt in plan.notes:
        print(f"  • {nt}")
    for wmsg in recipe.warnings_ru:
        print(f"  ! {wmsg}")
    if show_queries:
        for q in plan.queries:
            print(f"\n// ---- {q.purpose} ----\n{q.query}")
    print()


def print_stats(stats: dict[str, dict]) -> None:
    head = f"{'layer':<22}" + "".join(f"{c:>12}" for c in STAT_COLUMNS)
    print("\n" + head)
    for lyr, st in stats.items():
        print(f"{lyr:<22}" + "".join(f"{st[c]:>12}" for c in STAT_COLUMNS))


# ------------------------------------------------------------------ main pipeline
def run(args) -> int:
    run_id = str(uuid.uuid4())
    started = now_utc()
    config = load_config(args.config)
    if args.cache_dir:
        config.cache.dir = args.cache_dir
    if args.no_cache:
        config.cache.enabled = False
    if args.endpoint:
        config.overpass.endpoints = [args.endpoint]
    log_file = setup_logging(args.log_level, config.logs_dir(), run_id)

    if args.list_recipes:
        for r in list_recipes(config.recipe_dirs):
            print(f"{r.id:<24} v{r.version}  {r.label_ru}")
            for o in r.outputs:
                flag = "" if (o.enabled and not o.enabled_if) else "  (опционально)"
                print(f"    - {o.layer:<22} {o.geometry_type}{flag}")
        return 0

    is_tty = sys.stdin.isatty()
    if (not (args.aoi or args.bbox) or not args.recipe) and is_tty and not args.dry_run:
        args = interactive(args, config)
    if not (args.aoi or args.bbox):
        raise ArgumentError("Не задан AOI", action="Укажите --aoi PATH или --bbox W S E N")
    if args.aoi and args.bbox:
        raise ArgumentError("Укажите либо --aoi, либо --bbox")
    if not args.recipe:
        raise ArgumentError("Не задан рецепт", action="Укажите --recipe; список: --list-recipes")

    log.debug("run_id=%s %s %s Python %s %s", run_id, SOFTWARE_NAME, __version__, platform.python_version(),
              {k: v for k, v in vars(args).items()})
    recipe = load_recipe(args.recipe, config.recipe_dirs, allow_raw=args.allow_raw_query)
    recipe = apply_overrides(recipe, _params(args.param), args.enable_layer, args.disable_layer,
                             args.countries.split(",") if args.countries else None)
    log.info("Рецепт %s v%s (sha256 %s)", recipe.id, recipe.version, (recipe.sha256 or "")[:12])
    for wmsg in recipe.warnings_ru:
        log.warning(wmsg)

    chooser = (lambda layers: _choose("2) Слой AOI", layers)) if is_tty and not args.dry_run else None
    if args.bbox:
        aoi = aoi_from_bbox(*args.bbox)
        aoi.mode = "envelope"
    else:
        aoi = read_aoi(args.aoi, args.aoi_layer, args.aoi_crs, args.aoi_mode, chooser)

    backend, notes = choose_backend(args.backend, aoi, config, args.force)
    for nt in notes:
        log.warning(nt)
    cache_root = config.cache_dir()
    cache = ResponseCache(cache_root, enabled=config.cache.enabled and not args.dry_run,
                          ttl_days=config.cache.ttl_days)
    tag = recipe_fingerprint(recipe)
    if args.dry_run:
        provider_endpoint = config.overpass.endpoints[0]
        header = f"[out:json][timeout:{config.overpass.timeout_seconds}][maxsize:{config.overpass.maxsize_bytes}];"
        if backend not in ("overpass", "pbf"):
            get_provider(backend, config, cache, tag)  # покажет понятную ошибку
    else:
        # PBF: данные из выгрузок Geofabrik, Overpass — только для лёгких вспомогательных запросов
        provider = get_provider("overpass" if backend == "pbf" else backend, config, cache, tag)
        provider_endpoint = provider.endpoint
        header = provider.header()

    target = output_path(args, recipe, aoi)
    planner = QueryPlanner(config, cache)
    from .providers.overpass import light_header
    plan = planner.plan(aoi, recipe, backend, light_header(config.overpass), provider_endpoint)
    plan.notes = notes + plan.notes
    if backend == "pbf":
        plan.notes = [nt for nt in plan.notes if not nt.startswith(("Ускорение", "Этап 2"))] + [
            f"PBF: выгрузки Geofabrik скачиваются в {target.parent / '_osm_pbf'} и удаляются после обработки",
            "PBF: страна объектов — по линиям госграниц (несколько лёгких запросов Overpass)"]
    print_plan(plan, aoi, recipe, target, show_queries=args.dry_run)
    if args.dry_run:
        print("--dry-run: сеть не использовалась.")
        return 0
    check_target(target, args.overwrite or config.output.overwrite)
    if is_tty and not args.yes and len(plan.tiles) > 4:
        if _ask(f"Выполнить {len(plan.tiles)} тайловых запросов? (y/n)", "y").lower() not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return 0

    # --- загрузка
    if backend == "pbf":
        from .pbf_backend import PBFExecutor
        executor = PBFExecutor(config, provider, cache, recipe, header, aoi, target,
                               batch_recipes=_batch_recipes(args, config, aoi))
    else:
        executor = Executor(config, provider, cache, recipe, header)
    store = executor.run(plan.tiles)
    downloaded_at = now_utc()

    # --- сборка слоёв
    keep_tags = (recipe.keep_tags_json if recipe.keep_tags_json is not None else config.output.keep_tags_json) \
        and not args.no_tags_json
    ctx = BuildContext(recipe=recipe, store=store, aoi_geom=aoi.geometry, backend=backend,
                       downloaded_at=downloaded_at, clip_override=args.clip, keep_tags_json=keep_tags)
    layers, stats, expected, clipped_layers = {}, {}, {}, set()
    for out in recipe.active_outputs():
        extra_fields = None
        cands = None
        if out.transform == "state_borders":
            cands, extra_fields = state_border_candidates(out, ctx), STATE_BORDER_FIELDS
        elif out.transform == "country_outlines":
            cands = country_outline_candidates(out, ctx)
        gdf, st = build_layer(out, ctx, cands, extra_fields)
        layers[out.layer] = (gdf, out.geometry_type, out.label_ru or out.layer)
        stats[out.layer] = st
        expected[out.layer] = out.geometry_type
        clip_on = args.clip if args.clip is not None else (
            out.clip_geometry if out.clip_geometry is not None else recipe.clip_geometry)
        if clip_on and "clip" in out.op_names():
            clipped_layers.add(out.layer)
    apply_links(recipe, layers, stats)
    _run_checks(recipe, store)

    total = sum(s["written"] for s in stats.values())
    status = "ok" if total else "no_features"
    finished = now_utc()
    out_crs = args.output_crs or config.output.crs
    tables = _meta_tables(run_id, recipe, aoi, backend, provider, executor, started, finished, out_crs,
                          status, stats, store, config, args, cache)
    writer = GeoPackageWriter(target, crs=out_crs, spatial_index=config.output.spatial_index)
    tmp = writer.write(layers, tables)
    rep = validate_gpkg(tmp, expected, stats, aoi.geometry, clipped_layers, crs=out_crs,
                        spatial_index=config.output.spatial_index)
    print_stats(stats)
    if not rep.ok:
        for e in rep.errors:
            log.error("QC: %s", e)
        raise QualityError("Контроль качества не пройден", cause="; ".join(rep.errors[:5]),
                           action=f"Промежуточный файл сохранён: {tmp}")
    final = writer.commit()
    if backend == "pbf" and not args.pbf_batch:
        from .pbf_backend import cleanup, pbf_dir_for
        cleanup(pbf_dir_for(target), keep_pbf=args.keep_pbf or config.pbf.keep_files)
    if args.keep_raw and cache.enabled:
        log.info("Сырые ответы: %s", cache.keep_raw(run_id))
    size_mb = final.stat().st_size / 1048576
    log.info("Готово: %s (%.2f МБ), QC: %d проверок, предупреждений: %d", final, size_mb, rep.checks,
             len(store.warnings))
    print(f"\nРезультат: {final}  ({size_mb:.2f} МБ)")
    print(f"Атрибуция: {ATTRIBUTION}; {LICENSE}")
    if log_file:
        print(f"Журнал: {log_file}")
    if status == "no_features":
        print("Объектов в AOI не найдено: создан GeoPackage с пустыми слоями (status=no_features).")
        if args.fail_on_empty:
            raise QualityError("Результат пуст (--fail-on-empty)")
    return 0


def _batch_recipes(args, config, aoi) -> list[Recipe]:
    """Рецепты других заданий пакета с тем же AOI — отбор из PBF выполняется для них сразу."""
    if not args.pbf_batch:
        return []
    try:
        jobs = json.loads(Path(args.pbf_batch).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Пакет заданий PBF не прочитан (%s)", exc)
        return []
    out = []
    for argv in jobs:
        try:
            a = build_parser().parse_args(argv)
            if (a.aoi, a.aoi_layer, a.aoi_mode, a.bbox) != (args.aoi, args.aoi_layer, args.aoi_mode, args.bbox):
                continue
            r = load_recipe(a.recipe, config.recipe_dirs, allow_raw=a.allow_raw_query)
            out.append(apply_overrides(r, _params(a.param), a.enable_layer, a.disable_layer,
                                       a.countries.split(",") if a.countries else None))
        except Exception as exc:  # noqa: BLE001
            log.debug("пакет PBF: задание %s пропущено (%s)", argv, exc)
    return out


def _run_checks(recipe: Recipe, store) -> None:
    for ch in recipe.checks:
        if ch.type == "orphan_members":
            member_ways = set()
            for key in store.keys_of(ch.relations):
                el = store.elements.get(key) or {}
                member_ways |= {int(m["ref"]) for m in el.get("members") or [] if m.get("type") == "way"}
            n = 0
            for key in store.keys_of(ch.source):
                if key[0] == "way" and key[1] not in member_ways:
                    store.warn(ch.code, "", f"w{key[1]}", ch.message_ru)
                    n += 1
            if n:
                log.warning("%d объектов: %s", n, ch.message_ru)


def _meta_tables(run_id, recipe, aoi, backend, provider, executor, started, finished, out_crs, status,
                 stats, store, config, args, cache) -> dict[str, pd.DataFrame]:
    w, s, e, n = aoi.envelope_wsen
    wkt = aoi.geometry.wkt
    if len(wkt) > config.output.store_aoi_wkt_max_chars:
        wkt = "sha256:" + hashlib.sha256(wkt.encode()).hexdigest()
    qlist = [{"purpose": q["purpose"], "hash": q["hash"], "query": q["query"]} for q in executor.queries_log]
    all_q = json.dumps(qlist, ensure_ascii=False)
    base_ts = sorted(store.data_timestamps)
    eps = sorted({x["endpoint"] for x in getattr(provider, "executed", [])})
    warnings_text = list(recipe.warnings_ru)
    meta = pd.DataFrame([{
        "run_id": run_id, "recipe_id": recipe.id, "recipe_version": recipe.version,
        "recipe_label_ru": recipe.label_ru, "recipe_sha256": recipe.sha256,
        "recipe_params": json.dumps(recipe.params, ensure_ascii=False),
        "started_at_utc": started.isoformat(), "finished_at_utc": finished.isoformat(),
        "aoi_source": aoi.source, "aoi_layer": aoi.layer, "aoi_crs": aoi.crs, "aoi_mode": aoi.mode,
        "bbox_w": w, "bbox_s": s, "bbox_e": e, "bbox_n": n,
        "bbox_overpass_swne": ";".join(b.overpass() for b in aoi.bboxes),
        "aoi_wkt_4326": wkt,
        "backend": backend,
        "endpoint_or_source": ";".join(([executor.source_desc] if getattr(executor, "source_desc", "") else [])
                                       + eps) or provider.endpoint,
        "query_hash": query_hash(all_q), "query_text": all_q,
        "queries_count": len(qlist), "cache_hits": cache.hits,
        "source_data_timestamp": (base_ts[0] if len(base_ts) == 1 else
                                  (f"{base_ts[0]}..{base_ts[-1]}" if base_ts else None)),
        "clip_mode": ("no_clip" if args.clip is False else
                      ("clip" if args.clip else ("recipe_default:" + ("clip" if recipe.clip_geometry else "no_clip"))))
        + f"/{aoi.mode}",
        "output_crs": out_crs, "software_version": f"{SOFTWARE_NAME} {__version__}",
        "python_version": platform.python_version(),
        "license": LICENSE, "attribution": ATTRIBUTION,
        "disclaimer_ru": " ".join(warnings_text) or None,
        "status": status, "warnings_count": len(store.warnings),
    }])
    st = pd.DataFrame([{"layer": k, **v} for k, v in stats.items()],
                      columns=["layer"] + STAT_COLUMNS)
    lim = config.output.max_warning_rows
    wr = pd.DataFrame([{"code": c, "layer": lyr or None, "osm_uid": uid, "message": txt}
                       for c, lyr, uid, txt in store.warnings[:lim]],
                      columns=["code", "layer", "osm_uid", "message"])
    if wr.empty:
        wr = pd.DataFrame({"code": pd.Series([], dtype="object"), "layer": pd.Series([], dtype="object"),
                           "osm_uid": pd.Series([], dtype="object"), "message": pd.Series([], dtype="object")})
    if len(store.warnings) > lim:
        log.warning("Предупреждений %d, в GPKG записано %d (полный список — в журнале)", len(store.warnings), lim)
    for c, lyr, uid, txt in store.warnings:
        log.debug("WARN %s %s %s %s", c, lyr, uid, txt)
    return {"osm_download_metadata": meta, "osm_layer_statistics": st, "osm_processing_warnings": wr}


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raw = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    if args.gui or (not raw or raw in (["--config", args.config],)) and not args.console:
        from .gui import run_gui
        return run_gui(args.config)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nПрервано пользователем. Готовые тайлы сохранены в кэше.", file=sys.stderr)
        return 130
    except OSMDwnlError as exc:
        log.error(exc.describe()) if log.handlers else print(exc.describe(), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # непредвиденная ошибка: полный traceback в журнал
        log.exception("Непредвиденная ошибка: %s", exc)
        return 1
