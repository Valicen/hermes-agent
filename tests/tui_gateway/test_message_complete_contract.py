"""The message.complete contract must match Talaria's served-model producer."""

from tui_gateway.contracts.registry import EVENTS


def test_message_complete_accepts_served_model_stamp():
    payload = EVENTS["message.complete"].payload
    assert payload is not None
    parsed = payload.model_validate(
        {"text": "done", "status": "complete", "model": "gpt-6-astra"}
    )
    assert parsed.model_dump()["model"] == "gpt-6-astra"
