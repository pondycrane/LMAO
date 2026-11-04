"""Unit tests for tools/calendar_pics_sync.py (pure logic; no network/DB)."""

import json
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

    def test_juliet_varies_across_events_not_always_pikachu(self):
        picked = {cps.pick_subject("juliet", "tennis", f"uid{i}") for i in range(40)}
        assert len(picked) > 3
        assert picked != {"pikachu"}

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

    def test_wesley_theme_alternates_paw_and_sea(self):
        # the old bug pinned every event to the sea-animal theme
        themes = {cps.pick_theme("wesley", "tennis", f"uid{i}")[0]
                  for i in range(40)}
        assert themes == {"sea animal", "paw patrol"}
        paw = {cps.pick_subject("wesley", "tennis", f"uid{i}") for i in range(40)
               if cps.pick_theme("wesley", "tennis", f"uid{i}")[0] == "paw patrol"}
        assert len(paw) > 1  # a random PAW character, not always one dog

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
        c = cps.build_combo("W ice skating 1:30", "wesley", "uid1")
        assert c["queries"][0].endswith("ice skating")
        assert c["subject"] in cps.SEA_COHERENT["ice skating"] or \
            c["subject"] in cps.PAW_SUBJECTS
        assert c["theme"] in ("sea animal", "paw patrol")

    def test_juliet_tennis_query_pairs_subject_and_action(self):
        c = cps.build_combo("J tennis 4pm Mon", "juliet", "uid2")
        assert c["theme"] == "pokemon"
        assert c["queries"][0].endswith("tennis")
        assert c["queries"][0].startswith(c["subject"])
        assert c["subject"] in cps.POKE_SUBJECTS

    def test_primary_in_queries(self):
        c = cps.build_combo("Swim meet", "wesley", "uid3")
        assert c["queries"][0].endswith("swimming")
        assert c["queries"][0] in c["queries"]

    def test_llm_cannot_override_the_chosen_subject(self, monkeypatch):
        """The model used to answer 'pikachu' for every pokemon event; the
        seeded subject must win and queries for other characters be dropped."""
        subject = cps.pick_subject("juliet", "tennis", "uid2")
        monkeypatch.setenv("LMAO_IMAGE_LLM_URL", "http://llm.invalid/v1")
        monkeypatch.setattr(cps, "_llm_combo", lambda *a, **k: {
            "subject": "pikachu", "action": "tennis",
            "queries": ["pikachu tennis", f"{subject} playing tennis"],
        })
        c = cps.build_combo("J tennis 4pm Mon", "juliet", "uid2")
        assert c["subject"] == subject
        assert all(cps._norm_title(subject) in cps._norm_title(q)
                   for q in c["queries"])
        assert "pikachu tennis" not in [q.lower() for q in c["queries"]]

    def test_llm_action_refines_the_primary_query(self, monkeypatch):
        monkeypatch.setenv("LMAO_IMAGE_LLM_URL", "http://llm.invalid/v1")
        subject = cps.pick_subject("wesley", "tennis", "uid9")
        monkeypatch.setattr(cps, "_llm_combo", lambda *a, **k: {
            "action": "playing tennis", "queries": [f"{subject} playing tennis"],
        })
        c = cps.build_combo("W tennis 4pm", "wesley", "uid9")
        assert c["subject"] == subject
        assert c["action"] == "playing tennis"
        assert c["queries"][0] == f"{subject} playing tennis"


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
    @pytest.fixture(autouse=True)
    def _default_model(self, monkeypatch):
        # these assert the FLUX natural-language prompt
        monkeypatch.delenv("LMAO_IMAGE_GEN_MODEL", raising=False)

    def test_sea_animal_theme(self):
        p = cps.build_gen_prompt("seal", "sea animal", "ice skating")
        assert "seal" in p and "ice skating" in p
        assert "line art" in p and "white" in p

    def test_pokemon_theme_and_no_action(self):
        p = cps.build_gen_prompt("pikachu", "pokemon", "")
        assert "pikachu" in p and "fun activity" in p

    def test_pokemon_prompt_names_the_character_type(self):
        # a bare name ("goldeen") reads as a random word to the generator
        p = cps.build_gen_prompt("goldeen", "pokemon", "tennis")
        assert "pokemon" in p.lower() and "goldeen" in p

    def test_paw_prompt_names_the_show(self):
        p = cps.build_gen_prompt("marshall", "paw patrol", "swimming")
        assert "PAW Patrol" in p and "marshall" in p

    def test_sea_prompt_stays_bare(self):
        assert "pokemon" not in cps.build_gen_prompt("seal", "sea animal", "").lower()


class TestFluxWorkflow:
    @pytest.fixture(autouse=True)
    def _default_model(self, monkeypatch):
        monkeypatch.delenv("LMAO_IMAGE_GEN_MODEL", raising=False)

    def test_default_model_is_flux(self):
        assert cps.gen_model() == "flux1-schnell.safetensors"
        assert cps.gen_style() == "flux"

    def test_graph_is_the_flux_transformer_graph(self):
        wf = cps._flux_workflow("line art", 1024, 1024,
                                "flux1-schnell.safetensors")
        assert wf["1"]["class_type"] == "UNETLoader"
        assert wf["1"]["inputs"]["unet_name"] == "flux1-schnell.safetensors"
        assert wf["7"]["inputs"]["steps"] == 4


class TestAnimagineModel:
    """Animagine XL is tag-trained — the FLUX natural-language prompt reads
    badly to it, so the prompt style and the graph must both switch."""

    @pytest.fixture(autouse=True)
    def _select_animagine(self, monkeypatch):
        monkeypatch.setenv("LMAO_IMAGE_GEN_MODEL",
                           "animagine-xl-3.0-base.safetensors")

    def test_style_is_sdxl_anime(self):
        assert cps.gen_style() == "sdxl-anime"

    def test_tag_order_subject_then_series(self):
        # Animagine's caption template: subject, series, everything else
        tags = [t.strip() for t in
                cps.build_gen_prompt("pikachu", "pokemon", "tennis").split(",")]
        assert tags[:3] == ["no humans", "pikachu", "pokemon"]
        assert "tennis" in tags

    def test_lineart_tags_steer_the_1bit_tile(self):
        p = cps.build_gen_prompt("seal", "sea animal", "")
        assert "monochrome" in p and "lineart" in p
        assert "rating: general" in p and "no humans" in p

    def test_graph_loads_the_selected_checkpoint(self):
        wf = cps._sdxl_workflow("x", 1024, 1024,
                                "animagine-xl-3.0-base.safetensors")
        assert wf["1"]["class_type"] == "CheckpointLoaderSimple"
        assert wf["1"]["inputs"]["ckpt_name"] == \
            "animagine-xl-3.0-base.safetensors"

    def test_sampler_follows_the_model_card(self):
        # Euler Ancestral, <30 steps, CFG 5-7
        ks = cps._sdxl_workflow("x", 1024, 1024, "a.safetensors")["5"]["inputs"]
        assert ks["sampler_name"] == "euler_ancestral"
        assert ks["steps"] == 28
        assert 5.0 <= ks["cfg"] <= 7.0

    def test_negative_prompt_excludes_nsfw_and_humans(self):
        neg = cps._sdxl_workflow("x", 1024, 1024, "a.safetensors")["3"]["inputs"]["text"]
        assert "nsfw" in neg and "rating: sensitive" in neg
        assert "1girl" in neg and "photograph" in neg


class TestPerThemeModel:
    """A theme override picks the model that actually knows the characters:
    Animagine knows Pokémon, FLUX knows PAW Patrol (Danbooru has ~20 posts per
    PAW dog, so the anime checkpoint draws a generic animal)."""

    @pytest.fixture(autouse=True)
    def _route(self, monkeypatch):
        monkeypatch.delenv("LMAO_IMAGE_GEN_MODEL", raising=False)
        monkeypatch.setenv("LMAO_IMAGE_GEN_MODEL_POKEMON",
                           "animagine-xl-3.0.safetensors")

    def test_theme_override_wins(self):
        assert cps.gen_model("pokemon") == "animagine-xl-3.0.safetensors"
        assert cps.gen_style("pokemon") == "sdxl-anime"

    def test_other_themes_keep_the_default(self):
        assert cps.gen_model("paw patrol") == "flux1-schnell.safetensors"
        assert cps.gen_model("sea animal") == "flux1-schnell.safetensors"

    def test_env_name_normalizes_spaces(self):
        assert cps._theme_model_env("paw patrol") == \
            "LMAO_IMAGE_GEN_MODEL_PAW_PATROL"

    def test_prompt_style_follows_the_routed_model(self):
        # Pokémon -> tag prompt, PAW -> natural language, in one process
        assert "monochrome" in cps.build_gen_prompt("pikachu", "pokemon", "")
        assert "line art" in cps.build_gen_prompt("chase", "paw patrol", "")

    def test_generate_local_sends_the_routed_checkpoint(self, monkeypatch):
        sent = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b""

        def fake_urlopen(req, timeout=0):
            if req.full_url.endswith("/prompt"):
                sent["wf"] = json.loads(req.data)["prompt"]
            raise OSError("stop after capture")

        monkeypatch.setattr(cps.urllib.request, "urlopen", fake_urlopen)
        assert cps.generate_local("x", 1024, 1024, theme_label="pokemon") is None
        assert sent["wf"]["1"]["inputs"]["ckpt_name"] == \
            "animagine-xl-3.0.safetensors"
        assert cps.generate_local("x", 1024, 1024,
                                  theme_label="paw patrol") is None
        assert sent["wf"]["1"]["inputs"]["unet_name"] == \
            "flux1-schnell.safetensors"

    def test_paw_tag_is_series_qualified(self):
        # Danbooru's canonical form; a bare "marshall" reads as a name
        p = cps.build_tag_prompt("marshall", "paw patrol", "soccer")
        assert "marshall (paw patrol)" in p
        assert p.count("paw patrol") == 1


class TestBackfillWindow:
    def test_since_days_zero_is_future_only(self):
        import time

        cal = _FakeCal([])
        cps.kid_events_needing_pics(cal, _FakePics([]), force=True, since_days=0)
        cutoff = cal.queries[-1][1][0]
        # future-only cutoff is "now" (allow the call's own elapsed time)
        assert abs(cutoff - time.time() * 1000) < 5000

    def test_since_days_includes_recent_past(self):
        import time

        cal = _FakeCal([])
        cps.kid_events_needing_pics(cal, _FakePics([]), force=True, since_days=14)
        cutoff = cal.queries[-1][1][0]
        assert abs(cutoff - (time.time() - 14 * 86400) * 1000) < 5000

    def test_force_ignores_already_stored_keys(self):
        rows = [("u1", "J tennis", "ev_1", ["juliet"]),
                ("u2", "W swim", "ev_2", ["wesley"])]
        pics = _FakePics(["ev_1"])
        assert cps.kid_events_needing_pics(_FakeCal(rows), pics) == [rows[1]]
        assert cps.kid_events_needing_pics(_FakeCal(rows), pics, force=True) == rows

    def test_limit_caps_the_batch(self):
        rows = [(f"u{i}", f"event {i}", f"ev_{i}", ["juliet"]) for i in range(5)]
        got = cps.kid_events_needing_pics(_FakeCal(rows), _FakePics([]),
                                          force=True, limit=2)
        assert got == rows[:2]


class _FakeCursor:
    def __init__(self, rows, queries):
        self._rows, self._queries = rows, queries

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._queries.append((sql, params))

    def fetchall(self):
        return self._rows


class _FakeCal:
    def __init__(self, rows):
        import threading

        self._lock, self._conn, self.queries = threading.Lock(), self, []
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows, self.queries)

    def close(self):
        pass


class _FakePics:
    def __init__(self, keys):
        self._keys = keys

    def keys(self):
        return self._keys

    def close(self):
        pass


class TestVerifyPrompt:
    """The gate must be theme-level for pokemon, or valid renders get rejected
    and the retry loop collapses onto whichever character happens to pass."""

    def test_pokemon_accepts_any_pokemon_not_just_the_named_one(self):
        p = cps.build_verify_prompt("charmander", "pokemon", accept=cps.POKE_SUBJECTS)
        assert "Pokémon character" in p
        assert "any Pokémon character is acceptable" in p
        assert "'charmander'" in p          # names the seeded pick as a hint
        assert "photograph" in p and "costume" in p   # keeps cosplay photos out

    def test_pokemon_prompt_is_theme_level_even_with_a_pool(self):
        # the pool must not narrow it back to name-exact matching
        a = cps.build_verify_prompt("goldeen", "pokemon", accept=["goldeen"])
        assert "any Pokémon character is acceptable" in a

    def test_paw_prompt_lists_the_dogs(self):
        p = cps.build_verify_prompt("marshall", "paw patrol", accept=cps.PAW_SUBJECTS)
        assert "PAW Patrol" in p
        for dog in ("marshall", "chase", "skye", "rubble", "zuma"):
            assert dog in p
        assert "real dog photo" in p

    def test_sea_prompt_uses_the_coherent_pool(self):
        pool = cps.SEA_COHERENT["ice skating"]
        p = cps.build_verify_prompt("seal", "sea animal", accept=pool)
        assert "sea animals" in p
        for name in pool:
            assert name in p
        assert "dolphin" not in p   # legless animals stay out of ice skating

    def test_sea_prompt_defaults_to_the_full_pool(self):
        p = cps.build_verify_prompt("octopus", "sea animal")
        assert "octopus" in p and "whale" in p


class TestParseVerdict:
    def test_true_with_character(self):
        assert cps._parse_verdict('{"match": true, "character": "Psyduck"}') == (True, "Psyduck")

    def test_false_with_character(self):
        assert cps._parse_verdict('{"match": false, "character": "unknown"}') == (False, "unknown")

    def test_string_truthy(self):
        assert cps._parse_verdict('{"match": "yes"}') == (True, "")

    def test_garbage_is_unparseable(self):
        assert cps._parse_verdict("no verdict here") == (None, "")

    def test_parse_match_still_returns_a_bool(self):
        assert cps._parse_match('{"match": true}') is True
        assert cps._parse_match("nope") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
