import pytest

from app.platforms import whatsapp


@pytest.mark.asyncio
async def test_whatsapp_send_message_stub_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError) as exc_info:
        await whatsapp.send_message("+919999999999", "Hello from tests")

    assert "WhatsApp credentials not yet configured" in str(exc_info.value)


def test_whatsapp_chunk_text_splits_long_messages() -> None:
    long_text = "x" * 8200
    chunks = whatsapp._chunk_text(long_text)

    assert len(chunks) == 3
    assert len(chunks[0]) == 4096
    assert len(chunks[1]) == 4096
    assert len(chunks[2]) == 8
