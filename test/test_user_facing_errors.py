from __future__ import annotations

import pytest

from services.protocol.user_facing_errors import map_user_facing_image_error


@pytest.mark.parametrize(
    "raw,expected_substr",
    [
        ("This request was rejected by the content policy filter.", "content policy"),
        ("续轮询预算不足（同步调用方等待预算已耗尽）", "longer than expected"),
        ("sS stage wall timeout (75s, elapsed 82.1s)", "longer than expected"),
        ("ChatGPT 生图超时：实际已等待 5.0 秒（本次墙钟预算 5 秒）", "longer than expected"),
        ("no available image quota", "quota"),
        ("duplicate prompt within 120s window", "duplicate prompt"),
        ("Instant limit reached; limit resets in 2h.", "Instant limit"),
    ],
)
def test_map_user_facing_image_error(raw: str, expected_substr: str) -> None:
    mapped = map_user_facing_image_error(raw)
    assert expected_substr.lower() in mapped.lower()
