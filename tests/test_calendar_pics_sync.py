"""Unit tests for tools/calendar_pics_sync.py (pure logic; no network/DB)."""

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

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


class TestLineArtGate:
    def _png(self, grid):
        h, w = len(grid), len(grid[0])
        im = Image.new("L", (w, h), 255)
        px = im.load()
        for y, row in enumerate(grid):
            for x, v in enumerate(row):
                if v:
                    px[x, y] = 0
        buf = io.BytesIO()
        im.point(lambda v: 255 if v >= 128 else 0, mode="1").save(buf, format="PNG")
        return buf.getvalue()

    def _outline_box(self, size=20, inset=2):
        """A hollow box: outline ink with a big enclosed white interior."""
        return [[1 if (x in (inset, size - 1 - inset) or y in (inset, size - 1 - inset))
                 else 0 for x in range(size)] for y in range(size)]

    def _solid_box(self, size=20, inset=2):
        """A filled silhouette: lots of ink, no enclosed white."""
        return [[1 if (inset <= x <= size - 1 - inset and inset <= y <= size - 1 - inset)
                 else 0 for x in range(size)] for y in range(size)]

    def test_outline_drawing_is_accepted(self):
        assert cps.is_line_art(self._png(self._outline_box()))

    def test_filled_silhouette_is_rejected(self):
        # a coloring page with filled shapes: 0.35 ink but ~no interior
        png = self._png(self._solid_box())
        assert cps.ink_fraction(png) > cps.LINE_ART_MIN_INK
        assert cps.enclosed_white_fraction(png) < cps.LINE_ART_MIN_ENCLOSED
        assert not cps.is_line_art(png)

    def test_blank_page_is_rejected(self):
        # a badly cropped coloring page: tiny drawing, no enclosed area
        grid = [[0] * 20 for _ in range(20)]
        grid[9][9] = 1
        assert not cps.is_line_art(self._png(grid))

    def test_thresholded_photo_is_rejected(self):
        # a photo thresholded to 1-bit is nearly solid ink (~0.96)
        grid = [[1 if (x + y) % 17 else 0 for x in range(20)] for y in range(20)]
        assert not cps.is_line_art(self._png(grid))

    def test_measurements_match_pixels(self):
        png = self._png(self._outline_box())
        # 20x20, ring at inset 2 (x/y = 2 and 17) -> 14x14 = 196 enclosed px,
        # 76 ring px of ink.
        assert abs(cps.enclosed_white_fraction(png) - 196 / 400) < 1e-9
        assert abs(cps.ink_fraction(png) - 76 / 400) < 1e-9


class TestRasterizer:
    def test_thin_line_survives_downscale(self):
        """A 1-px line in a large source must still show at 176x176.

        The old path (LANCZOS downscale, then threshold) averaged thin lines
        into gray and dropped them.
        """
        src = Image.new("L", (1760, 1760), 255)
        px = src.load()
        for y in range(1760):
            px[880, y] = 0  # 1-px vertical line
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        tile = cps.to_1bit_png(buf.getvalue(), 176, 176, cps._THRESHOLD)
        with Image.open(io.BytesIO(tile)) as im:
            im = im.convert("L")
            ink_cols = [x for x in range(176)
                        if any(im.getpixel((x, y)) < 128 for y in range(176))]
        assert ink_cols == [88]  # exactly one tile column carries the line

    def test_rasterizer_output_is_1bit(self):
        src = Image.new("L", (352, 352), 255)
        px = src.load()
        for x in range(40, 300):
            for y in (40, 300):
                px[x, y] = 0
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        tile = cps.to_1bit_png(buf.getvalue(), 176, 176, cps._THRESHOLD)
        with Image.open(io.BytesIO(tile)) as im:
            assert im.mode == "1"
            assert im.size == (176, 176)


class TestThemeQuery:
    def test_character_theme_names_the_show(self):
        assert cps.theme_query("chase", "paw patrol", "tennis") == \
            "chase paw patrol tennis"

    def test_pokemon_theme_names_the_franchise(self):
        assert cps.theme_query("pikachu", "pokemon", "") == "pikachu pokemon"

    def test_sea_animal_stays_bare(self):
        assert cps.theme_query("dolphin", "sea animal", "swimming") == \
            "dolphin swimming"


class TestPickSource:
    def test_prefers_line_art_over_a_photo_of_the_same_subject(self):
        results = [_r("Chase tennis player photo.jpg"),
                   _r("Chase Paw Patrol Coloring Page.png")]
        picked = cps.pick_source(results, "chase tennis")
        assert "coloring" in picked[0].lower()

    def test_prefers_title_matching_query_token(self):
        results = [_r("Monster Ball Tour 2009.jpg"),
                   _r("Poké Ball.svg", w=120, h=120),
                   _r("Pokebola-pokeball-png-0.png")]
        picked = cps.pick_source(results, "pokeball")
        assert "pokebola" in picked[0].lower() or "poké ball" in picked[0]

    def test_accent_normalization_matches(self):
        results = [_r("Generic photo.jpg"), _r("Poké Ball.png")]
        picked = cps.pick_source(results, "pokeball")
        assert "pok" in picked[0].lower().replace("é", "e")
        assert "photo" not in picked[0].lower()

    def test_rejects_giant_maps(self):
        results = [_r("Diagram of everything.png", w=9000, h=9000),
                   _r("Sea map.png", w=8000, h=6000),
                   _r("Dolphin logo.png", w=512, h=282)]
        assert "dolphin" in cps.pick_source(results, "dolphin")[0].lower()

    def test_rejects_svg_without_a_rasterizer(self):
        # the sync image has no cairosvg; an SVG candidate would fail to decode
        assert cps.pick_source([_r("Dolphin line drawing.svg")], "dolphin") is None

    def test_prefers_query_with_more_tokens(self):
        results = [_r("Seal animal.jpg"), _r("Seal ice skating cartoon.png")]
        picked = cps.pick_source(results, "seal ice skating")
        assert "skating" in picked[0].lower()

    def test_falls_back_when_no_anchor(self):
        assert cps.pick_source([_r("A.png", w=100, h=100), _r("B.png", w=800, h=600)], "zzz")


class TestGoogleSource:
    def test_google_removed(self):
        # Google CSE is discontinued for new signups — search_sources uses
        # Wikimedia Commons + DuckDuckGo image search
        assert "search_google" not in dir(cps)


class TestWebSource:
    def test_no_local_generator_remains(self):
        # tiles are fetched line drawings; the ComfyUI generation path is gone
        assert not hasattr(cps, "generate_local")
        assert not hasattr(cps, "build_gen_prompt")

    def test_ddg_results_map_to_title_url_size(self, monkeypatch):
        monkeypatch.setattr(cps, "_ddg_vqd", lambda q: "4-123")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"results":[{"title":"Chase coloring page",' \
                       b'"image":"https://x/chase.png","width":1024,"height":1536},' \
                       b'{"title":"no url","image":"","width":1,"height":1}]}'

        monkeypatch.setattr(cps.urllib.request, "urlopen", lambda *a, **k: _Resp())
        got = cps.search_web_images("chase line drawing")
        assert got == [("Chase coloring page", "https://x/chase.png", 1024, 1536)]

    def test_search_sources_uses_one_themed_line_art_hint(self, monkeypatch):
        queries = []
        monkeypatch.setattr(cps, "search_commons", lambda q: queries.append(q) or [])
        monkeypatch.setattr(
            cps, "search_web_images",
            lambda q: queries.append(q) or [("t", "https://x/a.png", 800, 800)])
        got = cps.search_sources("seal ice skating", "sea animal")
        # one web call per query keeps the hourly run inside DuckDuckGo's limit
        assert queries == ["seal ice skating", "seal ice skating line drawing"]
        assert len(got) == 1

    def test_character_theme_searches_coloring_pages(self, monkeypatch):
        queries = []
        monkeypatch.setattr(cps, "search_commons", lambda q: [])
        monkeypatch.setattr(cps, "search_web_images",
                            lambda q: queries.append(q) or [])
        cps.search_sources("chase paw patrol tennis", "paw patrol")
        assert queries == ["chase paw patrol tennis coloring page"]
        cps.search_sources("pikachu pokemon", "pokemon")
        assert queries[-1] == "pikachu pokemon coloring page"

    def test_search_sources_survives_a_web_search_failure(self, monkeypatch):
        monkeypatch.setattr(cps, "search_commons",
                            lambda q: [("Dolphin.png", "https://x/d.png", 512, 512)])

        def boom(q):
            raise RuntimeError("rate limited")

        monkeypatch.setattr(cps, "search_web_images", boom)
        assert cps.search_sources("dolphin") == [
            ("Dolphin.png", "https://x/d.png", 512, 512)]

    def test_paw_patrol_is_searched_not_skipped(self, monkeypatch):
        """PAW tiles used to be generation-only; now every theme is searched."""
        seen = []
        monkeypatch.setattr(cps, "search_commons", lambda q: seen.append(q) or [])
        monkeypatch.setattr(cps, "search_web_images",
                            lambda q: seen.append(q) or [])
        cps.search_sources("chase tennis")
        assert "chase tennis" in seen


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
