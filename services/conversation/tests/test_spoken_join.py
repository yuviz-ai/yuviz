"""Stored spoken pieces are joined by _join_sentences: no space after a CJK sentence end, one otherwise."""
from services.conversation.pipeline import _join_sentences, _spoken_reply


def test_an_interrupted_cjk_reply_has_no_spaces_between_sentences():
    assert _spoken_reply([_join_sentences(["你好。", "我可以帮你。"])], []) == "你好。我可以帮你。"


def test_an_interrupted_latin_reply_has_single_spaces_between_sentences():
    assert _spoken_reply([_join_sentences(["Hello there.", "How can I help?"])], []) == "Hello there. How can I help?"
