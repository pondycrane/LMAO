"""Unit tests for tools/calendar_pics_sync.py (pure logic; no network/DB)."""

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools import calendar_pics_sync as cps  # noqa: E402


def _r(title, url=None, w=512, h=512):
    return (title, url or ("https://x/" + title), w, h)


class TestActionExtraction:
    def test_recognizes_activity_in_title(self):
        assert cps.extract_action("W ice skating 1:30") == "ice skating"
        assert cps.extract_action("J tennis 4pm Mon") == "tennis"
        assert cps.extract_action("Swim meet") == "swimming"
        assert cps.extract_action("Juliet dentist") == "dentist"

    def test_no_activity_gives_empty(self):
        assert cps.extract_action("Parent team lunch") == ""


class TestSubjectSelection:
    def test_juliet_picks_pokemon_pool(self):
        assert cps.pick_subject("juliet", "tennis", "uid1") in cps.POKE_SUBJECTS

    def test_wesley_ice_skating_is_limbed_not_legless(self):
        # a legless fish/dolphin must NOT be picked for ice skating
        for i in range(30):
            s = cps.pick_subject("wesley", "ice skating", f"uid{i}")
            assert s in cps.SEA_COHERENT["ice skating"]
            assert s not in ("dolphin", "whale", "shark", "fish")

    def test_wesley_swimming_allows_any_sea_animal(self):
        assert cps.pick_subject("wesley", "swimming", "u1") in cps.SEA_SUBJECTS

    def test_seeded_choice_is_deterministic_per_uid(self):
        assert cps._seeded_choice(cps.POKE_SUBJECTS, "abc") == \
            cps._seeded_choice(cps.POKE_SUBJECTS, "abc")


class TestCombo:
    @pytest.fixture(autouse=True)
    def _force_deterministic(self, monkeypatch):
        # never hit the live LLM hook in tests; exercise the reasoned fallback
        monkeypatch.delenv("LMAO_IMAGE_LLM_URL", raising=False)
        monkeypatch.delenv("LMAO_IMAGE_LLM_MODEL", raising=False)

    def test_wesley_ice_skating_query_is_coherent(self):
        q = cps.build_combo("W ice skating 1:30", "wesley", "uid1")
        assert q[0].endswith("ice skating")
        assert q[0].split()[0] in cps.SEA_COHERENT["ice skating"]

    def test_juliet_tennis_query_pairs_subject_and_action(self):
        q = cps.build_combo("J tennis 4pm Mon", "juliet", "uid2")
        assert q[0].endswith("tennis")
        assert q[0].split()[0] in cps.POKE_SUBJECTS

    def test_primary_in_queries(self):
        q = cps.build_combo("Swim meet", "wesley", "uid3")
        assert q[0].endswith("swimming")
        assert q[0] in q


class TestExtractJsonArray:
    def test_plain_array(self):
        assert cps._extract_json_array('["seal ice skating", "penguin on ice"]') == \
            ["seal ice skating", "penguin on ice"]

    def test_code_fence_wrapped(self):
        s = '```json\n["pikachu tennis"]\n```'
        assert cps._extract_json_array(s) == ["pikachu tennis"]

    def test_prose_around_array(self):
        assert cps._extract_json_array('Here: ["a", "b"].') == ["a", "b"]

    def test_garbage_returns_empty(self):
        assert cps._extract_json_array("no array here") == []


class TestPickSource:
    def test_prefers_title_matching_query_token(self):
        results = [_r("Monster Ball Tour 2009.jpg"),
                   _r("Poké Ball.svg", w=120, h=120),
                   _r("Pokebola-pokeball-png-0.png")]
        picked = cps.pick_source(results, "pokeball")
        assert "pokebola" in picked[0].lower() or "poké ball" in picked[0]

    def test_accent_normalization_matches(self):
        results = [_r("Generic photo.jpg"), _r("Poké Ball.svg")]
        picked = cps.pick_source(results, "pokeball")
        assert "pok" in picked[0].lower().replace("é", "e")
        assert "photo" not in picked[0].lower()

    def test_rejects_giant_maps(self):
        results = [_r("Diagram of everything.svg", w=9000, h=9000),
                   _r("Sea map.png", w=8000, h=6000),
                   _r("Dolphin logo.svg", w=512, h=282)]
        assert "dolphin" in cps.pick_source(results, "dolphin")[0].lower()

    def test_prefers_query_with_more_tokens(self):
        results = [_r("Seal animal.jpg"), _r("Seal ice skating cartoon.png")]
        picked = cps.pick_source(results, "seal ice skating")
        assert "skating" in picked[0].lower()

    def test_falls_back_when_no_anchor(self):
        assert cps.pick_source([_r("A.png", w=100, h=100), _r("B.png", w=800, h=600)], "zzz")


class TestGoogleSource:
    def test_google_disabled_without_env(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CSE_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CSE_CX", raising=False)
        assert cps.search_google("seal ice skating") == []
