"""GUI: формирование заданий «AOI × рецепт» (пропускается без дисплея)."""
import os

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture
def app(tmp_path, monkeypatch):
    import osmdwnl_core.gui as G
    monkeypatch.setattr(G, "SETTINGS", tmp_path / "s.json")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("нет дисплея")
    a = G.App(root)
    yield a
    root.destroy()


def test_jobs_matrix(app, tmp_path):
    app._add_aoi("a.gpkg", "lyr1")
    app._add_aoi("b.shp", None)
    for r in app.rec_vars:
        app.rec_vars[r].set(r in ("hydro", "state_borders"))
    app.param_vars["state_borders"]["mode"].set("shared_only")
    app.out_var.set(str(tmp_path))
    app.clip_var.set("обрезать по AOI")
    jobs = app.build_jobs(dry=False)
    assert len(jobs) == 4
    argv = dict(jobs)[next(k for k, _ in jobs if k.startswith("state_borders") and "a.gpkg" in k)]
    assert ["--aoi-layer", "lyr1"] == argv[argv.index("--aoi-layer"):argv.index("--aoi-layer") + 2]
    assert "mode=shared_only" in argv and "--clip" in argv and "-y" in argv
    assert argv[argv.index("--countries") + 1] == "RU,KZ"
    app.countries_var.set("ru; by,  UA")
    argv = app.build_jobs(dry=False)[0][1]
    assert argv[argv.index("--countries") + 1] == "RU,BY,UA"
    app.countries_var.set("RUS")
    with pytest.raises(ValueError):
        app.build_jobs(dry=False)


def test_jobs_require_aoi_and_recipe(app):
    for r in app.rec_vars:
        app.rec_vars[r].set(False)
    with pytest.raises(ValueError):
        app.build_jobs(dry=True)
    app.rec_vars["hydro"].set(True)
    with pytest.raises(ValueError):
        app.build_jobs(dry=True)
