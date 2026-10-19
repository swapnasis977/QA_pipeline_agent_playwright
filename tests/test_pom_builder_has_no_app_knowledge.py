"""
The engine holds no project knowledge: pom_builder used to bake one application's
quirks (a swallowed "New Registration" click, its "Failed to initiate" dialog, its
cleanup route naming) into every generated routes.ts, so a second application
inherited behaviour that made no sense for it. Those now come from the route
map's reserved `_options` key, and slider words from the profile.
"""
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import pom_builder  # noqa: E402
from utils import route_map  # noqa: E402

_ROUTES = {"home": {"url": "app/home", "clicks": []},
           "tool": {"url": "", "clicks": ["Open", "css=#go"]}}


def _helper(routes):
    return pom_builder.build_route_helper(routes)["content"]


def test_a_map_without_options_carries_no_application_knowledge():
    content = _helper(_ROUTES)
    for text in ("New Registration", "Failed to initiate", "Measured on this app"):
        assert text not in content
    assert "const RECLICKS = 0;" in content          # re-click is opt-in
    assert "const APP_ERROR: RegExp | null = null;" in content
    assert "startsWith('zz-cleanup-')" in content    # neutral default convention


def test_options_configure_reclick_error_regex_and_cleanup_prefix():
    routes = dict(_ROUTES)
    routes["_options"] = {"reclick": {"after_ms": 4000, "max": 3},
                          "app_error_regex": "Could not start", "cleanup_prefix": "undo-",
                          "route_attempts": 5}
    content = _helper(routes)
    assert "const RECLICK_AFTER = 4000;" in content and "const RECLICKS = 3;" in content
    assert 'new RegExp("Could not start", \'i\')' in content
    assert "startsWith('undo-')" in content and "const ROUTE_ATTEMPTS = 5;" in content
    assert "_options" not in pom_builder.build_route_helper(routes)["route_names"]


def test_options_travel_with_a_loaded_route_map(tmp_path, monkeypatch):
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"_options": {"reclick": True},
                                "tool": {"clicks": ["Open"]}}), encoding="utf-8")
    monkeypatch.setitem(os.environ, "APP_ROUTE_MAP", str(path))
    loaded = route_map.load(force=True)
    assert list(loaded) == ["tool"]                   # never returned as a route
    assert route_map.options() == {"reclick": True}
    assert "const RECLICKS = 2;" in pom_builder.build_route_helper(loaded)["content"]


def test_slider_detection_uses_the_configured_words():
    plans = [{"elements": [{"logical_name": "Volume knob"}]}]
    assert pom_builder.build_slider_helper(plans) == {}
    assert pom_builder.build_slider_helper(plans, words=["volume"])["path"] == "src/utils/sliders.ts"
    # configured words REPLACE the defaults
    assert pom_builder.build_slider_helper(
        [{"elements": [{"logical_name": "Slider"}]}], words=["volume"]) == {}


def test_slider_words_come_from_the_profile_environment(monkeypatch):
    monkeypatch.setenv("QA_SLIDER_WORDS", "Volume, Gain")
    assert pom_builder.slider_words() == ("volume", "gain")
    monkeypatch.delenv("QA_SLIDER_WORDS")
    assert {"slider", "range"} <= set(pom_builder.slider_words())


def test_a_profile_sliders_template_replaces_the_builtin(tmp_path, monkeypatch):
    (tmp_path / "sliders.ts").write_text("// custom", encoding="utf-8")
    monkeypatch.setenv("QA_TEMPLATES_DIR", str(tmp_path))
    plans = [{"elements": [{"logical_name": "Slider"}]}]
    assert pom_builder.build_slider_helper(plans)["content"] == "// custom"
    assert "brightness" not in pom_builder._SLIDERS_TS.lower()
