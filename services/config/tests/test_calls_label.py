import pytest

from services.config.calls import _mode_of


@pytest.mark.parametrize("direction, label", [("inbound", "AI"), ("outbound", "Outbound"), ("test", "WebRTC")])
def test_mode_label_by_direction(direction, label):
    assert _mode_of({"direction": direction}) == label
