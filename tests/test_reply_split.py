from agent_bridge.runtime.dispatcher import split_reply


def test_split_reply_keeps_chunks_within_limit() -> None:
    chunks = split_reply("first paragraph\n\nsecond paragraph", 18)

    assert all(len(chunk) <= 18 for chunk in chunks)
    assert "first paragraph" in chunks[0]

