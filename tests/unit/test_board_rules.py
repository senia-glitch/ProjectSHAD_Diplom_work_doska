import pytest

from app.access import is_allowed, load_policy
from app.realtime.hub import KeyedRateLimiter, RateLimiter
from app.scenarios.boards import StrokeDTO


def test_policy_guest_only_listed_scenarios():
    assert is_allowed('guest', 'boards.collaborate')
    assert not is_allowed('guest', 'boards.delete')
    assert not is_allowed('admin', 'boards.create')


def test_broken_policy_is_rejected(tmp_path):
    bad = tmp_path / 'access.toml'
    bad.write_text('[roles.guest]\nscenarios = "all"\n', encoding='utf-8')
    with pytest.raises(RuntimeError):
        load_policy(bad)


def test_rate_limiters():
    limiter = RateLimiter(rate=0.0001, burst=3)
    assert [limiter.allow() for _ in range(4)] == [True, True, True, False]
    keyed = KeyedRateLimiter(per_minute=1, max_keys=2)
    assert keyed.allow('a') and not keyed.allow('a') and keyed.allow('b')


def test_stroke_validation():
    ok = StrokeDTO(id='abcdefgh', tool='pen', color='#AABBCC', width=3, pts=[1.234, 2, 3, 4])
    assert ok.pts == [1.2, 2, 3, 4]
    for bad in ({'pts': [1, 2, 3]}, {'pts': [float('inf'), 0]}, {'tool': 'bomb'}, {'color': 'red'}, {'width': 0}):
        with pytest.raises(ValueError):
            StrokeDTO(**{'id': 'abcdefgh', 'tool': 'pen', 'color': '#000000', 'width': 3, 'pts': [0, 0], **bad})
