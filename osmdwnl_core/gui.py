"""Графический интерфейс OSMdwnl (Tkinter): выбор AOI в проводнике, рецептов и папки вывода.

Запуск: ``python OSMdwnl.py`` без аргументов, ``python OSMdwnl.py --gui`` или из Jupyter:
``%run OSMdwnl.py``. Каждая пара «AOI × рецепт» выполняется как отдельный запрос и даёт
отдельный GeoPackage. Загрузка идёт в фоновом потоке, журнал выводится в окно.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import ATTRIBUTION, SOFTWARE_NAME, __version__
from .config import default_data_dir, load_config
from .recipes import LEGACY_IDS, list_recipes

VECTOR_TYPES = [
    ("Векторные данные", "*.gpkg *.shp *.geojson *.json *.kml *.kmz *.fgb *.gml *.tab *.mif *.sqlite"),
    ("GeoPackage", "*.gpkg"), ("Shapefile", "*.shp"), ("GeoJSON", "*.geojson *.json"),
    ("KML", "*.kml *.kmz"), ("Все файлы", "*.*"),
]
SETTINGS = default_data_dir() / "gui_settings.json"
SOURCE_CHOICES = {"авто (для больших AOI предложить PBF)": "auto", "Overpass API": "overpass",
                  "PBF Geofabrik (скачать выгрузки)": "pbf"}
CLIP_CHOICES = {"как в рецепте": None, "обрезать по AOI": "--clip", "сохранять объекты целиком": "--no-clip"}


class _QueueWriter(io.TextIOBase):
    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("log", s))
        return len(s)

    def isatty(self):
        return False


class App:
    def __init__(self, root: tk.Tk, config_path: str | None = None):
        self.root = root
        self.config_path = config_path
        self.config = load_config(config_path)
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self._dry = False
        self.stop_flag = threading.Event()
        self.aois: list[tuple[str, str | None]] = []      # (путь, слой)
        self.recipes = list_recipes(self.config.recipe_dirs)
        self.settings = self._load_settings()

        root.title(f"{SOFTWARE_NAME} {__version__} — загрузка слоёв OpenStreetMap")
        root.geometry("1060x800")
        root.minsize(820, 640)
        self._build()
        self.root.after(150, self._poll)

    # ------------------------------------------------------------ settings
    def _load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        data = {"last_dir": self.settings.get("last_dir"), "output": self.out_var.get(),
                "recipes": [r.id for r in self.recipes if self.rec_vars[r.id].get()],
                "aoi_mode": self.mode_var.get(), "clip": self.clip_var.get(),
                "crs": self.crs_var.get(), "overwrite": self.overwrite_var.get(),
                "countries": self.countries_var.get(), "source": self.source_var.get()}
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ layout
    def _build(self):
        pad = {"padx": 6, "pady": 4}
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(3, weight=1)

        # --- AOI
        fa = ttk.LabelFrame(main, text="1. Области интереса (AOI)", padding=6)
        fa.grid(row=0, column=0, sticky="nsew", **pad)
        fa.columnconfigure(0, weight=1)
        self.aoi_list = tk.Listbox(fa, height=7, selectmode="extended", activestyle="none")
        self.aoi_list.grid(row=0, column=0, columnspan=3, sticky="nsew")
        ttk.Button(fa, text="Добавить файлы...", command=self.add_files).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Button(fa, text="Удалить выбранные", command=self.remove_selected).grid(row=1, column=1, sticky="w")
        ttk.Button(fa, text="Очистить", command=self.clear_aoi).grid(row=1, column=2, sticky="e")
        fb = ttk.Frame(fa)
        fb.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.use_bbox = tk.BooleanVar(value=False)
        ttk.Checkbutton(fb, text="или bbox, EPSG:4326 (W S E N):", variable=self.use_bbox).pack(side="left")
        self.bbox_vars = [tk.StringVar() for _ in range(4)]
        for v in self.bbox_vars:
            ttk.Entry(fb, textvariable=v, width=7).pack(side="left", padx=2)

        mf = ttk.Frame(fa)
        mf.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(mf, text="Обрезка:").pack(side="left")
        self.mode_var = tk.StringVar(value=self.settings.get("aoi_mode", "envelope"))
        ttk.Radiobutton(mf, text="по экстенту", value="envelope", variable=self.mode_var).pack(side="left")
        ttk.Radiobutton(mf, text="по точной геометрии", value="geometry", variable=self.mode_var).pack(side="left")

        # --- рецепты
        frl = ttk.LabelFrame(main, text="2. Рецепты (каждый - отдельный GeoPackage)", padding=6)
        frl.grid(row=0, column=1, rowspan=2, sticky="nsew", **pad)
        frl.rowconfigure(0, weight=1)
        frl.columnconfigure(0, weight=1)
        # список рецептов прокручивается: окно не растёт при добавлении новых рецептов
        canvas = tk.Canvas(frl, highlightthickness=0, height=330)
        vsb = ttk.Scrollbar(frl, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        fr = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=fr, anchor="nw")
        fr.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))

        def _wheel(e):
            if e.num == 4 or getattr(e, "delta", 0) > 0:
                canvas.yview_scroll(-2, "units")
            else:
                canvas.yview_scroll(2, "units")
        for w in (canvas, fr):
            w.bind("<Enter>", lambda e: (canvas.bind_all("<MouseWheel>", _wheel),
                                         canvas.bind_all("<Button-4>", _wheel),
                                         canvas.bind_all("<Button-5>", _wheel)))
            w.bind("<Leave>", lambda e: (canvas.unbind_all("<MouseWheel>"), canvas.unbind_all("<Button-4>"),
                                         canvas.unbind_all("<Button-5>")))
        self.rec_canvas = canvas
        self.rec_vars: dict[str, tk.BooleanVar] = {}
        self.param_vars: dict[str, dict[str, tk.Variable]] = {}
        chosen = {LEGACY_IDS.get(x, x) for x in self.settings.get("recipes", [])}
        fc = ttk.Frame(fr)
        fc.pack(anchor="w", fill="x")
        ttk.Label(fc, text="Страны (коды ISO 3166-1, через запятую):").pack(side="left")
        self.countries_var = tk.StringVar(value=self.settings.get("countries", "RU, KZ"))
        ttk.Entry(fc, textvariable=self.countries_var, width=18).pack(side="left", padx=4)
        ttk.Label(fr, text="Например: RU, KZ, BY. Общие для всех рецептов.",
                  foreground="#555555").pack(anchor="w")
        for r in self.recipes:
            var = tk.BooleanVar(value=r.id in chosen)
            self.rec_vars[r.id] = var
            ttk.Checkbutton(fr, text=r.label_ru, variable=var).pack(anchor="w", pady=(4, 0))
            pv: dict[str, tk.Variable] = {}
            for name, default in r.params.items():
                label = r.param_labels_ru.get(name, name)
                row = ttk.Frame(fr)
                row.pack(anchor="w", padx=(24, 0))
                if isinstance(default, bool):
                    v = tk.BooleanVar(value=default)
                    ttk.Checkbutton(row, text=label, variable=v).pack(side="left")
                else:
                    v = tk.StringVar(value=str(default))
                    ttk.Label(row, text=label + ":", wraplength=440).pack(side="top", anchor="w")
                    if name in r.param_choices:
                        ttk.Combobox(row, textvariable=v, values=r.param_choices[name], state="readonly",
                                     width=28).pack(side="top", anchor="w", pady=2)
                    else:
                        ttk.Entry(row, textvariable=v, width=20).pack(side="left", padx=4)
                pv[name] = v
            self.param_vars[r.id] = pv
            for w in r.warnings_ru:
                ttk.Label(fr, text="! " + w, foreground="#8a5a00", wraplength=420).pack(anchor="w", padx=(24, 0))

        # --- вывод
        fo = ttk.LabelFrame(main, text="3. Результат", padding=6)
        fo.grid(row=1, column=0, sticky="nsew", **pad)
        fo.columnconfigure(1, weight=1)
        ttk.Label(fo, text="Папка:").grid(row=0, column=0, sticky="w")
        self.out_var = tk.StringVar(value=self.settings.get("output", ""))
        ttk.Entry(fo, textvariable=self.out_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(fo, text="Обзор...", command=self.pick_output).grid(row=0, column=2)
        ttk.Label(fo, text="Геометрия:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.clip_var = tk.StringVar(value=self.settings.get("clip", "как в рецепте"))
        ttk.Combobox(fo, textvariable=self.clip_var, values=list(CLIP_CHOICES), state="readonly",
                     width=26).grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(fo, text="CRS результата:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.crs_var = tk.StringVar(value=self.settings.get("crs", "EPSG:4326"))
        ttk.Entry(fo, textvariable=self.crs_var, width=14).grid(row=2, column=1, sticky="w", padx=4, pady=(4, 0))
        self.overwrite_var = tk.BooleanVar(value=self.settings.get("overwrite", True))
        ttk.Checkbutton(fo, text="Перезаписывать существующие файлы", variable=self.overwrite_var).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.nocache_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(fo, text="Не использовать кэш", variable=self.nocache_var).grid(
            row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(fo, text="Источник:").grid(row=5, column=0, sticky="w", pady=(4, 0))
        src = self.settings.get("source")
        self.source_var = tk.StringVar(value=src if src in SOURCE_CHOICES else next(iter(SOURCE_CHOICES)))
        ttk.Combobox(fo, textvariable=self.source_var, values=list(SOURCE_CHOICES), state="readonly",
                     width=36).grid(row=5, column=1, sticky="w", padx=4, pady=(4, 0))

        # --- кнопки
        fbtn = ttk.Frame(main)
        fbtn.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)
        self.btn_run = ttk.Button(fbtn, text="Запустить", command=lambda: self.start(dry=False))
        self.btn_run.pack(side="left")
        self.btn_plan = ttk.Button(fbtn, text="Показать план (без загрузки)", command=lambda: self.start(dry=True))
        self.btn_plan.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(fbtn, text="Остановить после текущего", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")
        ttk.Button(fbtn, text="Открыть папку результата", command=self.open_output).pack(side="right")
        self.progress = ttk.Progressbar(fbtn, mode="determinate", length=180)
        self.progress.pack(side="right", padx=8)

        # --- журнал
        fl = ttk.LabelFrame(main, text="Журнал", padding=4)
        fl.grid(row=3, column=0, columnspan=2, sticky="nsew", **pad)
        fl.rowconfigure(0, weight=1)
        fl.columnconfigure(0, weight=1)
        self.log = tk.Text(fl, wrap="none", height=14, font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        sy = ttk.Scrollbar(fl, orient="vertical", command=self.log.yview)
        sy.grid(row=0, column=1, sticky="ns")
        sx = ttk.Scrollbar(fl, orient="horizontal", command=self.log.xview)
        sx.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.log.tag_configure("err", foreground="#b00020")
        self.log.tag_configure("ok", foreground="#1b5e20")
        self.status = tk.StringVar(value=f"Готов. {ATTRIBUTION}")
        ttk.Label(main, textvariable=self.status, anchor="w").grid(row=4, column=0, columnspan=2, sticky="ew")

    # ------------------------------------------------------------ AOI
    def add_files(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="Выберите файлы AOI",
                                            initialdir=self.settings.get("last_dir") or str(Path.home()),
                                            filetypes=VECTOR_TYPES)
        if not paths:
            return
        self.settings["last_dir"] = str(Path(paths[0]).parent)
        if not self.out_var.get():
            self.out_var.set(str(Path(paths[0]).parent))
        from .aoi import list_vector_layers
        for p in paths:
            try:
                layers = list_vector_layers(p)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("AOI", f"Не удалось открыть {p}:\n{getattr(exc, 'message', exc)}",
                                     parent=self.root)
                continue
            if len(layers) > 1:
                chosen = self.choose_layers(p, layers)
                for lyr in chosen:
                    self._add_aoi(p, lyr)
            elif layers:
                self._add_aoi(p, None)

    def _add_aoi(self, path, layer):
        if (path, layer) in self.aois:
            return
        self.aois.append((path, layer))
        self.aoi_list.insert("end", f"{Path(path).name}" + (f"  /  {layer}" if layer else "") + f"   ({path})")

    def choose_layers(self, path, layers) -> list[str]:
        win = tk.Toplevel(self.root)
        win.title("Выбор слоёв")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=f"{Path(path).name} содержит несколько слоёв.\n"
                            "Выберите один или несколько (каждый - отдельный AOI):",
                  padding=8).pack(anchor="w")
        lb = tk.Listbox(win, selectmode="extended", height=min(15, len(layers)), width=50)
        for lyr in layers:
            lb.insert("end", lyr)
        lb.pack(fill="both", expand=True, padx=8)
        lb.selection_set(0)
        res: list[str] = []

        def ok():
            res.extend(layers[i] for i in lb.curselection())
            win.destroy()
        ttk.Button(win, text="OK", command=ok).pack(pady=8)
        lb.bind("<Double-Button-1>", lambda e: ok())
        self.root.wait_window(win)
        return res

    def remove_selected(self):
        for i in reversed(self.aoi_list.curselection()):
            self.aoi_list.delete(i)
            del self.aois[i]

    def clear_aoi(self):
        self.aoi_list.delete(0, "end")
        self.aois.clear()

    def pick_output(self):
        d = filedialog.askdirectory(parent=self.root, title="Папка для GeoPackage",
                                    initialdir=self.out_var.get() or self.settings.get("last_dir") or str(Path.home()))
        if d:
            self.out_var.set(d)

    def open_output(self):
        d = self.out_var.get()
        if not d or not Path(d).exists():
            return
        if sys.platform.startswith("win"):
            os.startfile(d)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])

    # ------------------------------------------------------------ jobs
    # ------------------------------------------------------------ источник: Overpass или PBF
    def _read_aois(self):
        from .aoi import aoi_from_bbox, read_aoi
        out = []
        if self.use_bbox.get():
            nums = [float(v.get().replace(",", ".")) for v in self.bbox_vars]
            out.append(("bbox", aoi_from_bbox(*nums)))
        for path, layer in self.aois:
            out.append((Path(path).name + (f"/{layer}" if layer else ""),
                        read_aoi(path, layer, None, self.mode_var.get(), None)))
        return out

    def choose_source(self) -> str | None:
        """'overpass' | 'pbf' | None (отмена). Для режима «авто» оценивает площадь и предлагает PBF."""
        choice = SOURCE_CHOICES.get(self.source_var.get(), "auto")
        if choice != "auto":
            return choice
        from .planner import QueryPlanner
        try:
            aois = self._read_aois()
        except Exception as exc:  # noqa: BLE001 — ошибку AOI покажет само задание
            self.q.put(("log", f"Оценка площади не выполнена: {exc}\n"))
            return "overpass"
        limit = self.config.planner.recommend_pbf_area_deg2
        areas = [(lbl, sum(b.area_deg2 for b in a.bboxes)) for lbl, a in aois]
        if not areas or max(x for _, x in areas) <= limit:
            return "overpass"
        recs = [r for r in self.recipes if self.rec_vars[r.id].get()]
        planner = QueryPlanner(self.config)
        tiles = sum(len(planner.tiles(a, r)) for _, a in aois for r in recs)
        self.root.configure(cursor="watch")
        self.status.set("Оценка объёма выгрузок PBF...")
        self.root.update_idletasks()
        from .pbf_backend import estimate
        lines_pbf, total, minutes, unknown = [], 0, 0.0, False
        try:
            for lbl, a in aois:
                est = estimate(a, self.config)
                total += est["bytes"]
                minutes += est["minutes"]
                unknown |= est["unknown"]
                lines_pbf.append(f"   {lbl}: " + ", ".join(
                    f"{x.id} ({x.size / 1048576:.0f} МБ)" if x.size else x.id for x in est["extracts"]))
        except Exception as exc:  # noqa: BLE001
            lines_pbf.append(f"   выгрузки не определены: {exc}")
            unknown = True
        finally:
            self.root.configure(cursor="")
            self.status.set("Готов.")
        out = self.out_var.get().strip() or "папку результата"
        lo, hi = tiles * 15 / 3600, tiles * 70 / 3600
        size_txt = f"{total / 1073741824:.1f} ГБ" if total else "размер неизвестен"
        if unknown and total:
            size_txt += " (без учёта части файлов)"
        msg = ("Площадь AOI: " + "; ".join(f"{lbl} — {x:.0f} град²" for lbl, x in areas) + ".\n\n"
               f"Overpass: около {tiles} тайловых запросов на все рецепты, ориентировочно "
               f"{lo:.1f}–{hi:.1f} ч (зависит от загрузки серверов).\n\n"
               f"PBF: скачать выгрузки Geofabrik ({size_txt}) в {Path(out) / '_osm_pbf'}:\n"
               + "\n".join(lines_pbf) +
               f"\nОтбор объектов — сразу для всех рецептов, ориентировочно {max(5, round(minutes / 5) * 5):.0f} мин "
               "(загрузка + обработка). После успешного завершения скачанные файлы удаляются.\n\n"
               "Использовать PBF?\nДа — PBF, Нет — Overpass, Отмена — не запускать.")
        try:
            from .pbf_backend import require_osmium
            require_osmium()
        except Exception:  # noqa: BLE001
            msg = msg.replace("Использовать PBF?", "Для PBF сначала установите пакет: pip install osmium\n\n"
                              "Использовать PBF?")
        ans = messagebox.askyesnocancel(SOFTWARE_NAME, msg, parent=self.root)
        if ans is None:
            return None
        return "pbf" if ans else "overpass"

    def build_jobs(self, dry: bool) -> list[tuple[str, list[str]]]:
        recs = [r for r in self.recipes if self.rec_vars[r.id].get()]
        if not recs:
            raise ValueError("Отметьте хотя бы один рецепт.")
        aoi_args: list[tuple[str, list[str]]] = []
        if self.use_bbox.get():
            try:
                nums = [float(v.get().replace(",", ".")) for v in self.bbox_vars]
            except ValueError:
                raise ValueError("bbox: введите четыре числа W S E N.") from None
            aoi_args.append((f"bbox {' '.join(map(str, nums))}", ["--bbox", *map(str, nums)]))
        for path, layer in self.aois:
            a = ["--aoi", path, "--aoi-mode", self.mode_var.get()]
            if layer:
                a += ["--aoi-layer", layer]
            aoi_args.append((Path(path).name + (f"/{layer}" if layer else ""), a))
        if not aoi_args:
            raise ValueError("Добавьте файлы AOI кнопкой «Добавить файлы...» или задайте bbox.")
        out = self.out_var.get().strip()
        if not out and not dry:
            raise ValueError("Укажите папку результата.")
        common = ["-y"]
        cc = [c.strip().upper() for c in re.split(r"[,;\s]+", self.countries_var.get()) if c.strip()]
        bad = [c for c in cc if not re.fullmatch(r"[A-Z]{2}", c)]
        if bad:
            raise ValueError(f"Неверный код страны: {', '.join(bad)}. Нужны двухбуквенные коды ISO, "
                             "например RU, KZ, BY.")
        if cc:
            common += ["--countries", ",".join(cc)]
        if out:
            common += ["--output", out]
        if self.config_path:
            common += ["--config", self.config_path]
        flag = CLIP_CHOICES.get(self.clip_var.get())
        if flag:
            common.append(flag)
        crs = self.crs_var.get().strip()
        if crs and crs.upper() != "EPSG:4326":
            common += ["--output-crs", crs]
        if self.overwrite_var.get():
            common.append("--overwrite")
        if self.nocache_var.get():
            common.append("--no-cache")
        if dry:
            common.append("--dry-run")
        jobs = []
        for label, a in aoi_args:
            for r in recs:
                params = []
                for name, var in self.param_vars[r.id].items():
                    val = var.get()
                    if isinstance(val, bool):
                        val = "true" if val else "false"
                    params += ["--param", f"{name}={val}"]
                jobs.append((f"{r.id}  x  {label}", [*a, "--recipe", r.id, *params, *common]))
        return jobs

    def start(self, dry: bool):
        if self.worker and self.worker.is_alive():
            return
        try:
            jobs = self.build_jobs(dry)
        except ValueError as exc:
            messagebox.showwarning(SOFTWARE_NAME, str(exc), parent=self.root)
            return
        self._save_settings()
        if dry:
            source = SOURCE_CHOICES.get(self.source_var.get(), "auto")
            source = None if source == "auto" else source
        else:
            source = self.choose_source()
            if source is None:
                return
        self._pbf_dirs: list[Path] = []
        if source:
            jobs = [(lbl, argv + ["--backend", source]) for lbl, argv in jobs]
        if source == "pbf" and not dry:
            from .pbf_backend import PBF_DIR_NAME
            folder = Path(self.out_var.get().strip())
            if folder.suffix.lower() == ".gpkg":
                folder = folder.parent
            folder = folder / PBF_DIR_NAME
            folder.mkdir(parents=True, exist_ok=True)
            batch = folder / "batch.json"
            batch.write_text(json.dumps([argv for _, argv in jobs], ensure_ascii=False), encoding="utf-8")
            jobs = [(lbl, argv + ["--pbf-batch", str(batch)]) for lbl, argv in jobs]
            self._pbf_dirs = [folder]
        self._dry = dry
        self.stop_flag.clear()
        self.log.delete("1.0", "end")
        self.progress.configure(maximum=len(jobs), value=0)
        for b in (self.btn_run, self.btn_plan):
            b.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.set(f"Выполняется: 0/{len(jobs)}")
        self.worker = threading.Thread(target=self._work, args=(jobs,), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag.set()
        self.status.set("Остановка после текущего запроса...")

    def _work(self, jobs):
        from .cli import main as cli_main
        writer = _QueueWriter(self.q)
        results = []
        for i, (label, argv) in enumerate(jobs, 1):
            if self.stop_flag.is_set():
                results.append((label, None, "пропущено"))
                continue
            self.q.put(("log", f"\n{'=' * 70}\n[{i}/{len(jobs)}] {label}\n{'=' * 70}\n"))
            captured = io.StringIO()

            class Tee(io.TextIOBase):
                def write(self, s):
                    writer.write(s)
                    captured.write(s)
                    return len(s)

                def isatty(self):
                    return False
            tee = Tee()
            try:
                with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                    code = cli_main(argv)
            except SystemExit as exc:  # argparse
                code = exc.code if isinstance(exc.code, int) else 2
            except Exception:  # noqa: BLE001
                writer.write(traceback.format_exc())
                code = 1
            m = re.findall(r"^Результат: (.+?\.gpkg)", captured.getvalue(), flags=re.M)
            results.append((label, code, m[-1] if m else ""))
            self.q.put(("progress", i, len(jobs)))
        for folder in getattr(self, "_pbf_dirs", []):
            if all(c == 0 for _, c, _ in results):
                from .pbf_backend import cleanup
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    cleanup(folder, keep_pbf=self.config.pbf.keep_files)
                self.q.put(("log", f"\nПромежуточные файлы PBF удалены ({folder}).\n"))
            else:
                self.q.put(("log", f"\nСкачанные выгрузки PBF оставлены в {folder} для повторного запуска; "
                                   "они удаляются после успешного завершения всех заданий.\n"))
        self.q.put(("done", results))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    txt = item[1]
                    tag = "err" if ("ERROR" in txt or "Traceback" in txt) else None
                    self.log.insert("end", txt, tag)
                    self.log.see("end")
                elif item[0] == "progress":
                    self.progress.configure(value=item[1])
                    self.status.set(f"Выполняется: {item[1]}/{item[2]}")
                elif item[0] == "done":
                    self._finish(item[1])
        except queue.Empty:
            pass
        self.root.after(150, self._poll)

    def _finish(self, results):
        for b in (self.btn_run, self.btn_plan):
            b.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        ok = sum(1 for _, c, _ in results if c == 0)
        lines = []
        for label, code, info in results:
            mark = "OK " if code == 0 else ("-- " if code is None else f"ОШИБКА (код {code}) ")
            if self._dry and code == 0:
                info = "план показан, файл не создавался"
            lines.append(f"{mark} {label}" + (f"\n      {info}" if info else ""))
        self.log.insert("end", "\nИтог:\n" + "\n".join(lines) + "\n", "ok" if ok == len(results) else "err")
        self.log.see("end")
        self.status.set(f"Готово: успешно {ok} из {len(results)}. {ATTRIBUTION}")
        if self._dry:
            return
        if ok == len(results):
            messagebox.showinfo(SOFTWARE_NAME, f"Готово: {ok} из {len(results)}.", parent=self.root)
        else:
            messagebox.showwarning(SOFTWARE_NAME, f"Успешно {ok} из {len(results)}. Подробности — в журнале.",
                                   parent=self.root)


def run_gui(config_path: str | None = None) -> int:
    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # чёткие шрифты на HiDPI
    except Exception:  # noqa: BLE001
        pass
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Графический интерфейс недоступен ({exc}). Используйте параметры командной строки, см. --help.")
        return 2
    try:
        ttk.Style(root).theme_use("vista" if sys.platform.startswith("win") else "clam")
    except tk.TclError:
        pass
    App(root, config_path)
    root.mainloop()
    return 0
