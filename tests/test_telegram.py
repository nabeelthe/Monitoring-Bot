from nuva_bot.telegram import _split, MAX_MSG_LEN


def test_split_short_passthrough():
    assert _split("hello") == ["hello"]


def test_split_long_message():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(100))
    chunks = _split(text)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_MSG_LEN for c in chunks)
    # no content lost
    assert sum(len(c.replace("\n", "")) for c in chunks) == len(text.replace("\n", ""))


def test_split_single_giant_line():
    text = "y" * (MAX_MSG_LEN * 2 + 100)
    chunks = _split(text)
    assert all(len(c) <= MAX_MSG_LEN for c in chunks)
    assert "".join(chunks) == text


def test_split_preserves_order_with_mixed_lines():
    text = "first\n" + "z" * (MAX_MSG_LEN + 50) + "\nlast"
    chunks = _split(text)
    joined = "".join(c.replace("\n", "") for c in chunks)
    assert joined.startswith("first")
    assert joined.endswith("last")
