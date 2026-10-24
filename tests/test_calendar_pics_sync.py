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
        # a legless fish/dolphin must NOT be picked for ice skating; PAW dogs ok
        for i in range(60):
            s = cps.pick_subject("wesley", "ice skating", f"uid{i}")
            assert s in cps.PAW_SUBJECTS or s in cps.SEA_COHERENT["ice skating"]
            assert s not in ("dolphin", "whale", "shark", "fish")

    def test_wesley_swimming_allows_any_sea_or_paw(self):
        for i in range(30):
            s = cps.pick_subject("wesley", "swimming", f"uid{i}")
            assert s in cps.SEA_SUBJECTS or s in cps.PAW_SUBJECTS

    def test_seeded_choice_is_deterministic_per_uid(self):
        assert cps._seeded_choice(cps.POKE_SUBJECTS, "abc") == \
            cps._seeded_choice(cps.POKE_SUBJECTS, "abc")


class TestSubjectPool:
    def test_pokemon_subject_maps_to_pokemon_pool(self):
        pool, theme = cps._pool_for("goldeen", "tennis")
        assert theme == "pokemon"
        assert "pikachu" in pool

    def test_paw_subject_maps_to_paw_pool(self):
        pool, theme = cps._pool_for("chase", "ice skating")
        assert theme == "paw patrol"
        assert "marshall" in pool

    def test_sea_subject_maps_to_coherent_pool(self):
        pool, theme = cps._pool_for("seal", "ice skating")
        assert theme == "sea animal"
        assert "penguin" in pool

    def test_two_word_sea_subject_still_maps_to_sea(self):
        _, theme = cps._pool_for("sea lion", "ice skating")
        assert theme == "sea animal"


class TestCombo:
    @pytest.fixture(autouse=True)
    def _force_deterministic(self, monkeypatch):
        # never hit the live LLM hook in tests; exercise the reasoned fallback
        monkeypatch.delenv("LMAO_IMAGE_LLM_URL", raising=False)
        monkeypatch.delenv("LMAO_IMAGE_LLM_MODEL", raising=False)

    def test_wesley_ice_skating_query_is_coherent(self):
        q = cps.build_combo("W ice skating 1:30", "wesley", "uid1")
        assert q[0].endswith("ice skating")
        lead = q[0].rsplit(" ice skating", 1)[0]  # subject; may be multi-word
        assert lead in cps.SEA_COHERENT["ice skating"] or lead in cps.PAW_SUBJECTS

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


class TestParseBbox:
    def test_parses_array(self):
        assert cps._parse_bbox("[100, 200, 300, 400]", 500, 500) == (100, 200, 300, 400)

    def test_clamps_to_image(self):
        # 2000 clamps to width 1000; still a real (non-full-frame) crop
        assert cps._parse_bbox("[50, 50, 2000, 300]", 1000, 500) == (50, 50, 1000, 300)

    def test_near_full_frame_is_none(self):
        assert cps._parse_bbox("[0, 0, 495, 495]", 500, 500) is None

    def test_tiny_box_is_none(self):
        assert cps._parse_bbox("[10, 10, 20, 20]", 500, 500) is None

    def test_garbage_is_none(self):
        assert cps._parse_bbox("I don't see a subject clearly", 500, 500) is None


class TestExtractJsonObject:
    def test_object(self):
        assert cps._extract_json_object('{"subject":"seal","queries":["a","b"]}') == \
            {"subject": "seal", "queries": ["a", "b"]}

    def test_code_fence(self):
        s = '```json\n{"subject":"pikachu"}\n```'
        assert cps._extract_json_object(s) == {"subject": "pikachu"}

    def test_prose_around(self):
        assert cps._extract_json_object('Here: {"a":1}.') == {"a": 1}

    def test_garbage(self):
        assert cps._extract_json_object("nothing here") == {}


class TestParseMatch:
    def test_true(self):
        assert cps._parse_match('{"match": true, "reason": "it is a pokemon"}') is True

    def test_false(self):
        assert cps._parse_match('{"match": false, "reason": "it is a boat"}') is False

    def test_unparseable_is_none(self):
        assert cps._parse_match("no verdict") is None


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
    def test_google_removed_commons_only(self):
        # Google CSE is discontinued for new signups — search_sources is Commons only
        assert "search_google" not in dir(cps)


class TestGenPrompt:
    def test_sea_animal_theme(self):
        p = cps.build_gen_prompt("seal", "sea animal", "ice skating")
        assert "seal" in p and "ice skating" in p
        assert "line art" in p and "white" in p

    def test_pokemon_theme_and_no_action(self):
        p = cps.build_gen_prompt("pikachu", "pokemon", "")
        assert "pikachu" in p and "fun activity" in p
