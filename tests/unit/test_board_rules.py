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


def test_chat_text_cleanup_and_tag():
    from app.scenarios.boards import AddChatDTO, author_tag, clean_chat_text
    assert clean_chat_text(' a\x00b \r\n\n\n\nc\t') == 'ab\n\nc'
    assert author_tag('abcdefgh') == author_tag('abcdefgh') != author_tag('abcdefgi')
    base = {'board_id': 'b' * 22, 'author': 'abcdefgh', 'name': 'A', 'color': '#112233', 'keep': 10}
    assert AddChatDTO(**base, text=' hi ').text == 'hi'
    for bad in ('  \n ', 'x' * 1001):
        with pytest.raises(ValueError):
            AddChatDTO(**base, text=bad)


def test_board_file_validation():
    from app.scenarios.boards import BoardExport
    ok = BoardExport.model_validate({'format': 'doska', 'version': 1, 'title': ' A  B ',
                                     'strokes': [{'tool': 'line', 'color': '#000000', 'width': 2, 'pts': [0, 0, 1, 1]}]})
    assert ok.title == 'A B' and ok.strokes[0].tool == 'line'
    for bad in ({'format': 'x'}, {'version': 2}, {'strokes': [{'tool': 'pen'}]}):
        with pytest.raises(ValueError):
            BoardExport.model_validate({'format': 'doska', 'version': 1, **bad})


def test_hub_exclusive_releases_room():
    import asyncio
    from app.realtime.hub import Hub

    async def scenario():
        hub = Hub()
        async with hub.exclusive('x' * 22) as room:
            assert room.lock.locked() and hub.rooms
        assert not hub.rooms
    asyncio.run(scenario())
