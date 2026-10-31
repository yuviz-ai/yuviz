"""Spoken system strings per language, falling back to English."""

from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from ..fillers import FillerSelector, _TOOL_FILLERS
from ..i18n import en, hi, t, tool_fillers
from .. import pipeline
from .. import i18n


def test_every_hindi_key_exists_in_english():
    assert set(hi.STRINGS) <= set(en.STRINGS)


def test_hindi_strings_are_used_for_hindi():
    assert t("fallback_goodbye", "hi") == hi.STRINGS["fallback_goodbye"]
    assert t("max_duration_goodbye", "hi-IN") == hi.STRINGS["max_duration_goodbye"]


@pytest.mark.parametrize("language", ["fr", "xx", None, ""])
def test_unknown_language_falls_back_to_english(language):
    assert t("fallback_goodbye", language) == "Goodbye."
    assert tool_fillers(language) == en.TOOL_FILLERS


def test_missing_key_in_a_language_falls_back_to_english(monkeypatch):
    monkeypatch.delitem(hi.STRINGS, "fallback_llm_error")
    assert t("fallback_llm_error", "hi") == en.STRINGS["fallback_llm_error"]


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        t("no_such_key", "en")


def test_english_pipeline_strings_are_byte_identical():
    assert pipeline._FALLBACK_GOODBYE == "Goodbye."
    assert pipeline._MAX_DURATION_GOODBYE == (
        "We're at the time limit for this call now. Thanks for calling — goodbye."
    )
    assert pipeline._FALLBACK_LLM_ERROR == (
        "Sorry, I'm having a little trouble right now. Could you say that again?"
    )
    assert pipeline._TRANSFER_FAILED_FALLBACK == "I'm sorry, I couldn't connect you to an agent right now."
    assert _TOOL_FILLERS[0] == ("One moment.", 1.0)


def test_filler_selector_uses_the_language_pool():
    phrases = {p for p, _ in hi.TOOL_FILLERS}
    for _ in range(20):
        assert FillerSelector().select_tool_filler("t", None, 1500, language="hi") in phrases
    assert FillerSelector().select_tool_filler("t", None, 1000) in {p for p, _ in en.TOOL_FILLERS}


_VALID_XX = '''
STRINGS = {"fallback_goodbye": "xx goodbye"}
TOOL_FILLERS = (("xx one", 1.0), ("xx two", 2.0))
'''


def _make_package(tmp_path, monkeypatch, modules: dict[str, str]):
    """A throwaway i18n-shaped package holding the real en.py plus the given modules."""
    name = f"i18n_{tmp_path.name.replace('-', '_')}"
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "en.py").write_text((Path(i18n.__file__).parent / "en.py").read_text())
    for module, source in modules.items():
        (pkg / f"{module}.py").write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    return name, pkg


@pytest.mark.parametrize(
    ("module", "source", "rule"),
    [
        ("xx", 'STRINGS = {"fallback_goodbye": "x"}', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = ()', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", "1.0"),)', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", float("nan")),)', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 0),)', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", True),)', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {"fallback_goodbye": ""}\nTOOL_FILLERS = (("xx", 1.0),)', "STRINGS must be a dict"),
        ("xx", 'STRINGS = {"fallback_godbye": "x"}\nTOOL_FILLERS = (("xx", 1.0),)', "keys not in en.STRINGS"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 1.0),)\nFIRST_TURN_FILLERS = None', "FIRST_TURN_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 1.0),)\nFIRST_TURN_FILLERS = ()', "FIRST_TURN_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 1.0),)\nFIRST_TURN_FILLERS = [("a", 1.0)]', "FIRST_TURN_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 1.0),)\nFIRST_TURN_FILLERS = (("", 1.0),)', "FIRST_TURN_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 1.0),)\nFIRST_TURN_FILLERS = ((1, 1.0),)', "FIRST_TURN_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 1.0),)\nFIRST_TURN_FILLERS = (("a", 0),)', "FIRST_TURN_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 1.0),)\nFIRST_TURN_FILLERS = (("a", float("inf")),)', "FIRST_TURN_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 1.0),)\nFIRST_TURN_FILLERS = (("a", True),)', "FIRST_TURN_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 1.0),)\nFIRST_TURN_FILLERS = (("a", 1.0, 2),)', "FIRST_TURN_FILLERS must be a non-empty tuple"),
        ("english", _VALID_XX, "name must be a 2-3 letter lowercase language code"),
    ],
)
def test_malformed_language_module_fails_discovery(tmp_path, monkeypatch, module, source, rule):
    name, pkg = _make_package(tmp_path, monkeypatch, {module: source})
    with pytest.raises(ValueError, match=rf"{module!r}.*{rule}"):
        i18n._discover([str(pkg)], name)


def test_module_without_first_turn_fillers_discovers_fine(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": _VALID_XX})
    assert set(i18n._discover([str(pkg)], name)) == {"en", "xx"}


def test_first_turn_accessors_for_a_module_without_the_table(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": _XX_THREE_TIERS})
    tables = i18n._discover([str(pkg)], name)
    shortest = {c: min(m.TOOL_FILLERS, key=lambda f: f[1])[0] for c, m in tables.items()}
    monkeypatch.setattr(i18n, "_TABLES", tables)
    monkeypatch.setattr(i18n, "_SHORTEST", shortest)
    monkeypatch.setattr(i18n, "_SHORTEST_FIRST_TURN", i18n._shortest_first_turn(tables, shortest))

    assert i18n.first_turn_fillers("xx") is None
    assert i18n.shortest_first_turn_filler("xx") == "xx short"


@pytest.mark.parametrize("language", ["fr", "zz", None])
def test_first_turn_accessors_for_a_language_without_a_module_use_english(language):
    assert i18n.first_turn_fillers(language) == i18n.first_turn_fillers("en")
    assert i18n.shortest_first_turn_filler(language) == i18n.shortest_first_turn_filler("en")


def test_discovery_requires_english(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": _VALID_XX})
    (pkg / "en.py").unlink()
    with pytest.raises(ValueError, match="en"):
        i18n._discover([str(pkg)], name)


def test_language_sorting_before_english_validates(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"ar": _VALID_XX})
    assert sorted(i18n._discover([str(pkg)], name)) == ["ar", "en"]


def test_valid_language_module_is_discovered(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": _VALID_XX, "_helper": "raise RuntimeError('imported')"})
    assert set(i18n._discover([str(pkg)], name)) == {"en", "xx"}


def test_real_package_discovers_every_language_file():
    files = {
        p.stem for p in Path(i18n.__file__).parent.glob("*.py") if not p.stem.startswith("_")
    }
    assert len(files) >= 2
    assert set(i18n._TABLES) == files


_XX_THREE_TIERS = '''
STRINGS = {"fallback_goodbye": "xx goodbye"}
TOOL_FILLERS = (("xx short", 1.0), ("xx mid", 2.0), ("xx long", 3.0))
'''


def test_discovered_language_is_wired_into_strings_selector_and_shortest(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": _XX_THREE_TIERS})
    tables = i18n._discover([str(pkg)], name)
    monkeypatch.setattr(i18n, "_TABLES", tables)
    monkeypatch.setattr(
        i18n, "_SHORTEST", {c: min(m.TOOL_FILLERS, key=lambda f: f[1])[0] for c, m in tables.items()}
    )

    assert t("fallback_goodbye", "xx") == "xx goodbye"
    assert t("fallback_llm_error", "xx") == tables["en"].STRINGS["fallback_llm_error"]
    for ms, phrase in [(500, "xx short"), (2000, "xx mid"), (9000, "xx long")]:
        assert FillerSelector().select_tool_filler("t", None, ms, language="xx") == phrase
    assert i18n.shortest_tool_filler("xx-YY") == "xx short"


def test_first_turn_selector_for_a_module_without_the_table_never_speaks_english(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": _XX_THREE_TIERS})
    tables = i18n._discover([str(pkg)], name)
    shortest = {c: min(m.TOOL_FILLERS, key=lambda f: f[1])[0] for c, m in tables.items()}
    monkeypatch.setattr(i18n, "_TABLES", tables)
    monkeypatch.setattr(i18n, "_SHORTEST", shortest)
    monkeypatch.setattr(i18n, "_SHORTEST_FIRST_TURN", shortest)

    phrase = FillerSelector().select_first_turn_filler(None, 1000, "xx")

    assert phrase == "xx short"
    assert phrase not in {p for p, _ in en.TOOL_FILLERS + en.FIRST_TURN_FILLERS}


@pytest.mark.parametrize("code", sorted(i18n._TABLES))
def test_selector_error_falls_back_in_the_sessions_language(code, monkeypatch):
    def boom(_):
        raise RuntimeError("boom")

    monkeypatch.setattr(random, "choice", boom)
    phrase = FillerSelector().select_tool_filler("t", None, 1500, language=code)
    assert phrase == i18n.shortest_tool_filler(code)
    assert phrase in {p for p, _ in i18n._TABLES[code].TOOL_FILLERS}


@pytest.mark.parametrize("language", ["fr", "zz", None])
def test_unknown_language_gets_english_everywhere(language):
    assert t("fallback_goodbye", language) == en.STRINGS["fallback_goodbye"]
    assert tool_fillers(language) == en.TOOL_FILLERS
    assert i18n.shortest_tool_filler(language) in {p for p, _ in en.TOOL_FILLERS}


def test_unknown_key_still_raises_for_a_discovered_language():
    with pytest.raises(KeyError):
        t("no_such_key", "hi")


def test_shortest_tool_filler_is_the_real_shortest_phrase():
    assert i18n.shortest_tool_filler("en") == "One moment."
    assert i18n.shortest_tool_filler("hi") == "एक पल।"


# Lookup and task verbs belong to TOOL_FILLERS: the turn-1 opener is spoken before the LLM has decided anything.
_BANNED_STEMS = {
    "en": ("check", "look into", "take care of", "thank"),
    "hi": ("देख", "पूरा कर", "धन्यवाद", "शुक्रिया"),
    "ta": ("பார்க்க", "சரிபார்", "முடித்து", "நன்றி"),
    "te": ("చూ", "పూర్తి", "ధన్యవాద"),
    "kn": ("ನೋಡ", "ಮುಗಿಸ", "ಧನ್ಯವಾದ"),
    "mr": ("पाह", "तपास", "पूर्ण कर", "धन्यवाद"),
    "bn": ("দেখ", "শেষ কর", "ধন্যবাদ"),
}
_LOOKUP_FILLER = {
    "en": "Let me check on that.", "hi": "जी, अभी देखते हैं।", "ta": "சரி, இப்போது பார்க்கலாம்.",
    "te": "సరే, ఇప్పుడే చూద్దాం.", "kn": "ಸರಿ, ಈಗ ನೋಡೋಣ.", "mr": "हो, आत्ता पाहूया.", "bn": "আচ্ছা, এখনই দেখছি।",
}
# Thanks belong to a closing or a wait the caller has already sat through, not to a reply to a greeting.
_THANKS_FILLER = {
    "en": "Thank you, one moment please.", "hi": "धन्यवाद, बस एक पल।", "ta": "நன்றி, ஒரு நிமிடம்.",
    "te": "ధన్యవాదాలు, ఒక్క క్షణం.", "kn": "ಧನ್ಯವಾದಗಳು, ಒಂದು ಕ್ಷಣ.", "mr": "धन्यवाद, एक क्षण.", "bn": "ধন্যবাদ, এক মুহূর্ত।",
}


# Every language that ships a turn-1 pool is checked, so a new language is covered without editing the tests.
_POOLED = sorted(code for code, module in i18n._TABLES.items() if getattr(module, "FIRST_TURN_FILLERS", None))


def _phrases(pool):
    return [phrase for phrase, _seconds in pool]


@pytest.mark.parametrize("code", _POOLED)
def test_first_turn_fillers_use_no_lookup_or_task_wording(code):
    module = i18n._TABLES[code]
    # A language with a turn-1 pool must declare its banned stems here, or its wording goes unchecked.
    assert code in _BANNED_STEMS, f"add lookup/task stems for {code!r} to _BANNED_STEMS and _LOOKUP_FILLER"
    stems = _BANNED_STEMS[code]
    # The matcher must be able to fire, or the assertion below proves nothing.
    assert _LOOKUP_FILLER[code] in _phrases(module.TOOL_FILLERS)
    assert any(stem in _LOOKUP_FILLER[code].lower() for stem in stems)
    assert any(stem in _THANKS_FILLER[code].lower() for stem in stems)
    for phrase in _phrases(module.FIRST_TURN_FILLERS):
        assert not any(stem in phrase.lower() for stem in stems), phrase


@pytest.mark.parametrize("code", _POOLED)
def test_first_turn_fillers_are_disjoint_from_tool_fillers_and_cover_the_shortest_tier(code):
    module = i18n._TABLES[code]
    assert set(_phrases(module.FIRST_TURN_FILLERS)).isdisjoint(_phrases(module.TOOL_FILLERS))
    assert min(s for _p, s in module.FIRST_TURN_FILLERS) <= min(s for _p, s in module.TOOL_FILLERS)


def test_the_pooled_languages_include_en_and_hi():
    assert {"en", "hi"} <= set(_POOLED)


def test_hindi_first_turn_fillers_have_no_latin_letters():
    for phrase in _phrases(hi.FIRST_TURN_FILLERS):
        assert not any("a" <= ch.lower() <= "z" for ch in phrase), phrase


def test_no_unrequested_language_modules_ship():
    assert set(i18n._TABLES).isdisjoint({"es", "fr", "de", "pt", "it", "ja", "zh"})


def test_lookups_do_no_module_discovery_at_call_time(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("discovery at call time")

    monkeypatch.setattr(i18n.pkgutil, "iter_modules", _boom)
    monkeypatch.setattr(i18n.importlib, "import_module", _boom)

    assert i18n.t("fallback_goodbye", "hi")
    assert i18n.tool_fillers("hi")
    assert i18n.shortest_tool_filler("hi")


def test_importing_the_package_registers_every_language_without_a_lookup():
    code = (
        "import sys, services.conversation.i18n as i;"
        "assert 'hi' in i._TABLES, sorted(i._TABLES);"
        "assert 'services.conversation.i18n.hi' in sys.modules"
    )
    root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONPATH": str(root)}
    done = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_module_without_phrase_tables_warns_once_and_falls_back_to_english(tmp_path, monkeypatch, caplog):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": _VALID_XX})
    with caplog.at_level("WARNING", logger=i18n.log.name):
        tables = i18n._discover([str(pkg)], name)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "'xx'" in warnings[0].getMessage()
    assert "CLOSURE_PHRASES" in warnings[0].getMessage() and "FAREWELL_PHRASES" in warnings[0].getMessage()

    monkeypatch.setattr(i18n, "_TABLES", tables)
    assert i18n.closure_phrases("xx") == tables["en"].CLOSURE_PHRASES
    assert i18n.farewell_phrases("xx") == tables["en"].FAREWELL_PHRASES


def test_deployed_languages_ship_their_own_phrase_tables():
    assert i18n.closure_phrases("hi") == hi.CLOSURE_PHRASES != en.CLOSURE_PHRASES
    assert i18n.farewell_phrases("hi") == hi.FAREWELL_PHRASES != en.FAREWELL_PHRASES


@pytest.mark.parametrize("bad", ['"bye"', "()", '("bye", 3)', '("",)'], ids=["str", "empty", "non-str", "blank"])
def test_malformed_phrase_table_fails_discovery(tmp_path, monkeypatch, bad):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": f"{_VALID_XX}\nCLOSURE_PHRASES = {bad}\n"})
    with pytest.raises(ValueError, match=r"'xx'.*CLOSURE_PHRASES must be a non-empty tuple"):
        i18n._discover([str(pkg)], name)


def test_english_must_define_phrase_tables(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {})
    (pkg / "en.py").write_text((pkg / "en.py").read_text().replace("FAREWELL_PHRASES", "OTHER_PHRASES"))
    with pytest.raises(ValueError, match=r"'en'.*FAREWELL_PHRASES must be a non-empty tuple"):
        i18n._discover([str(pkg)], name)
