from app.interfaces.discord_bot import split_message


def test_split_message_short_text() -> None:
    text = "hello"
    assert split_message(text, max_length=10) == [text]


def test_split_message_respects_max_length() -> None:
    text = " ".join(["word"] * 80)
    chunks = split_message(text, max_length=60)

    assert len(chunks) > 1
    assert all(len(chunk) <= 60 for chunk in chunks)

    # split_message may normalize edge whitespace at boundaries.
    reconstructed = " ".join(" ".join(chunks).split())
    assert reconstructed == " ".join(text.split())
