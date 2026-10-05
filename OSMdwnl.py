#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OSMdwnl.py — загрузка тематических слоёв OpenStreetMap для AOI в GeoPackage.

Тонкая точка входа; вся логика находится в папке-пакете ``osmdwnl_core`` рядом с этим файлом.

Без параметров открывается окно: выбор файлов AOI в проводнике, рецептов и папки.
    python OSMdwnl.py                 (или в Jupyter: %run OSMdwnl.py)
Командная строка:
    python OSMdwnl.py --aoi D:\\GIS\\aoi.gpkg --recipe hydro --output D:\\GIS\\OSM
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = "osmdwnl_core"


def _locate_package() -> str:
    """Папка, содержащая osmdwnl_core: рядом со скриптом или во вложенной папке после распаковки."""
    for cand in (HERE, os.path.join(HERE, "OSMdwnl"), os.getcwd()):
        if os.path.isfile(os.path.join(cand, PKG, "__init__.py")):
            return cand
    raise SystemExit(
        f"Не найдена папка '{PKG}' рядом с {os.path.join(HERE, 'OSMdwnl.py')}.\n"
        f"Ожидается структура:\n  {HERE}\\OSMdwnl.py\n  {HERE}\\{PKG}\\__init__.py, cli.py, gui.py ...\n"
        f"  {HERE}\\recipes\\*.yml\n"
        "Распакуйте архив OSMdwnl.zip целиком, сохранив все папки.")


sys.path.insert(0, _locate_package())
for _m in [m for m in sys.modules if m == PKG or m.startswith(PKG + ".")]:
    del sys.modules[_m]   # повторный %run в Jupyter подхватывает обновлённый код

from osmdwnl_core.cli import main  # noqa: E402


def _in_ipython() -> bool:
    return "ipykernel" in sys.modules or "IPython" in sys.modules


if __name__ == "__main__":
    code = main()
    if _in_ipython():
        # В Jupyter не вызываем sys.exit: иначе появляется «An exception has occurred…»
        if code:
            print(f"OSMdwnl завершился с кодом {code}")
    else:
        sys.exit(code)
