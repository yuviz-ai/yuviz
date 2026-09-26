import pytest

from .. import fillers as fillers_module
from ..fillers import _FALLBACK_FILLER, _TOOL_FILLERS, FillerSelector


def test_select_tool_filler_shorter_for_faster_average():
    selector = FillerSelector()
    fast = selector.select_tool_filler("book_appointment", None, 600.0)
    by_text = {text: seconds for text, seconds in _TOOL_FILLERS}
    selector2 = FillerSelector()
    slow = selector2.select_tool_filler("book_appointment", None, 2500.0)
    assert by_text[fast] < by_text[slow]


def test_select_tool_filler_never_repeats_back_to_back():
    # average_ms=2000.0 -> target=2.0s -> exactly one phrase fits
    # ("Sure, let me look into that.", 2.0s) -> without the last_phrase
    # exclusion filter, every call would return that same one phrase and
    # this assertion would trip. (600.0 -> target=0.6s previously produced
    # a 2-phrase tier via the "shortest" fallback branch, so rotation alone
    # already prevented repeats and this test could never actually fail —
    # deleting the last_phrase filter from fillers.py left it green.)
    selector = FillerSelector()
    last_phrase = None
    for _ in range(20):
        phrase = selector.select_tool_filler("book_appointment", last_phrase, 2000.0)
        assert phrase != last_phrase
        last_phrase = phrase


@pytest.mark.parametrize("average_ms", [None, 0, -1, float("nan")])
def test_select_tool_filler_handles_invalid_averages(average_ms):
    selector = FillerSelector()
    phrase = selector.select_tool_filler("book_appointment", None, average_ms)
    assert phrase


def test_select_tool_filler_fallback_on_raise(monkeypatch):
    monkeypatch.setattr(fillers_module, "_TOOL_FILLERS", None)
    selector = FillerSelector()
    assert selector.select_tool_filler("book_appointment", None, 600.0) == _FALLBACK_FILLER


