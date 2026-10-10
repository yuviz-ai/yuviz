from __future__ import annotations

from services.knowledge.chunking import chunk_text


def test_empty_text_returns_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_is_a_single_chunk():
    chunks = chunk_text("Refunds take 30 days to process.", chunk_size=200)
    assert len(chunks) == 1
    assert chunks[0].content == "Refunds take 30 days to process."
    assert chunks[0].token_count == 6


def test_paragraphs_pack_until_chunk_size_exceeded():
    para_a = " ".join(f"wordA{i}" for i in range(50))
    para_b = " ".join(f"wordB{i}" for i in range(50))
    para_c = " ".join(f"wordC{i}" for i in range(50))
    text = f"{para_a}\n\n{para_b}\n\n{para_c}"

    # a+b together (100 words) fit under 120; adding c would exceed it.
    chunks = chunk_text(text, chunk_size=120, chunk_overlap=0)

    assert len(chunks) == 2
    assert "wordA0" in chunks[0].content and "wordB0" in chunks[0].content
    assert "wordC0" in chunks[1].content


def test_overlap_repeats_trailing_words_into_next_chunk():
    para_a = " ".join(f"a{i}" for i in range(50))
    para_b = " ".join(f"b{i}" for i in range(50))
    text = f"{para_a}\n\n{para_b}"

    chunks = chunk_text(text, chunk_size=80, chunk_overlap=10)

    assert len(chunks) == 2
    tail_of_first = chunks[0].content.split()[-10:]
    head_of_second = chunks[1].content.split()[:10]
    assert tail_of_first == head_of_second


def test_single_paragraph_longer_than_chunk_size_still_splits():
    text = " ".join(f"word{i}" for i in range(500))
    chunks = chunk_text(text, chunk_size=100, chunk_overlap=0)
    assert len(chunks) >= 5
    for chunk in chunks:
        assert chunk.token_count <= 100


# ── Languages without spaces ─────────────────────────────────────────────────

from services.knowledge.chunking import count_tokens, is_unspaced  # noqa: E402


def test_cjk_is_sized_by_characters():
    text = "这是一个关于退款政策的句子。" * 100  # one 1400-char paragraph, no spaces
    chunks = chunk_text(text, chunk_size=100, chunk_overlap=0)
    assert len(chunks) > 1
    assert all(c.token_count <= 150 for c in chunks)
    assert "".join(c.content for c in chunks) == text


def test_cjk_overlap_is_in_characters():
    text = "あいうえおかきくけこ" * 40
    chunks = chunk_text(text, chunk_size=100, chunk_overlap=10)
    assert chunks[1].content.startswith(chunks[0].content[-15:])


def test_document_language_overrides_script_sniffing():
    assert is_unspaced("Refunds take 30 days.", "ja")
    assert not is_unspaced("退款需要三十天", "en")
    assert is_unspaced("退款需要三十天")
    assert not is_unspaced("Refunds take 30 days. 退款")


def test_thai_keeps_phrase_spaces():
    chunks = chunk_text("สวัสดีครับ ยินดีต้อนรับ", language="th")
    assert chunks[0].content == "สวัสดีครับ ยินดีต้อนรับ"
    assert chunks[0].token_count == count_tokens("สวัสดีครับ ยินดีต้อนรับ", "th") == len("สวัสดีครับยินดีต้อนรับ")


def test_hindi_still_chunks_by_words():
    chunks = chunk_text("रिफंड में तीस दिन लगते हैं", chunk_size=200, language="hi")
    assert chunks[0].token_count == 6
