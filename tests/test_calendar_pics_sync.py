"""Unit tests for tools/calendar_pics_sync.py (pure selection logic; no network/DB)."""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools import calendar_pics_sync as cps  # noqa: E402


def _r(title, url=None, w=512, h=512):
    return (title, url or ("https://x/" + title), w, h)


class TestPickSource:
    def test_prefers_title_matching_query_token(self):
        results = [
            _r("Monster Ball Tour 2009.jpg"),
            _r("Poké Ball.svg", w=120, h=120),
            _r("Pokebola-pokeball-png-0.png"),
        ]
        picked = cps.pick_source(results, "pokeball")
        # must pick a pokeball-named file, NOT the Monster Ball Tour photo
        assert "pokebola" in picked[0].lower() or "poké ball" in picked[0]

    def test_accent_normalization_matches(self):
        # "pokeball" query should match a title that spells it "Poké Ball"
        results = [_r("Generic photo.jpg"), _r("Poké Ball.svg")]
        picked = cps.pick_source(results, "pokeball")
        assert "pok" in picked[0].lower().replace("é", "e")
        assert "photo" not in picked[0].lower()

    def test_rejects_giant_maps(self):
        results = [_r("Diagram of everything.svg", w=9000, h=9000),
                   _r("Sea map.png", w=8000, h=6000),
                   _r("Dolphin logo.svg", w=512, h=282)]
        picked = cps.pick_source(results, "dolphin")
        assert "dolphin" in picked[0].lower()

    def test_falls_back_when_no_anchor(self):
        results = [_r("A.png", w=100, h=100), _r("B.png", w=800, h=600)]
        picked = cps.pick_source(results, "zzz")  # no token matches title
        assert picked is not None


class TestThemeFor:
    def test_maps_kids_to_themes(self):
        assert cps.theme_for(["juliet"]) == cps.CHILD_THEMES["juliet"][0]
        assert cps.theme_for(["wesley"]) == cps.CHILD_THEMES["wesley"][0]

    def test_first_kid_wins(self):
        assert cps.theme_for(["juliet", "wesley"]) == cps.CHILD_THEMES["juliet"][0]

    def test_unknown_kid_falls_back(self):
        assert isinstance(cps.theme_for(["nobody"]), str) and cps.theme_for(["nobody"])
