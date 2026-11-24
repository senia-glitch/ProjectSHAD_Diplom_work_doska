import asyncio
import json

import pytest

from app.access import is_allowed, load_policy
from app.realtime.guard import EntryGuard, FailureTracker
from app.realtime.hub import KeyedRateLimiter, LimitError, RateLimiter, Room
from app.scenarios.boards import (BoardFile, ChatTextDTO, CreateBoardDTO, StrokeDTO, author_tag, clean_chat_text,
                                  generate_code, hash_code, normalize_code, verify_code)


def test_policy_guest_only_listed_scenarios():
    assert is_allowed('guest', 'boards.join')
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
    assert ok.pts == [1.2, 2, 3, 4] and ok.color == '#aabbcc'
    assert StrokeDTO(id='abcdefgh', tool='eraser', color='#000000', width=500, pts=[0, 0]).width == 500
    for bad in ({'pts': [1, 2, 3]}, {'pts': [float('inf'), 0]}, {'tool': 'bomb'}, {'color': 'red'}, {'width': 0},
                {'width': 501}):
        with pytest.raises(ValueError):
            StrokeDTO(**{'id': 'abcdefgh', 'tool': 'pen', 'color': '#000000', 'width': 3, 'pts': [0, 0], **bad})


def test_access_code_rules():
    code = generate_code()
    assert len(code) == 8 and code == normalize_code(code)
    stored = hash_code(code)
    assert code not in stored and stored.startswith('scrypt$')
    assert verify_code(code.lower()[:4] + '-' + code.lower()[4:], stored)  # регистр и дефис не важны
    assert not verify_code('WRONG123', stored) and not verify_code(code, None) and not verify_code(code, 'garbage')
    assert CreateBoardDTO(author='abcdefgh', code=' my-code 42 ').code == 'MYCODE42'
    assert CreateBoardDTO(author='abcdefgh', code='  ').code is None
    for bad in ('abc', 'x' * 33, 'код!!'):
        with pytest.raises(ValueError):
            CreateBoardDTO(author='abcdefgh', code=bad)


def test_chat_text_rules():
    assert clean_chat_text(' a\x00b \r\n\n\n\nc\t') == 'ab\n\nc'
    assert author_tag('abcdefgh') == author_tag('abcdefgh') != author_tag('abcdefgi')
    assert ChatTextDTO(text=' hi ').text == 'hi'
    for bad in ('  \n ', 'x' * 1001):
        with pytest.raises(ValueError):
            ChatTextDTO(text=bad)


def test_board_file_validation():
    ok = BoardFile.model_validate({'format': 'doska', 'version': 1, 'title': ' A  B ',
                                   'strokes': [{'tool': 'line', 'color': '#000000', 'width': 2, 'pts': [0, 0, 1, 1]}]})
    assert ok.title == 'A B' and ok.strokes[0].tool == 'line'
    for bad in ({'format': 'x'}, {'version': 2}, {'strokes': [{'tool': 'pen'}]}):
        with pytest.raises(ValueError):
            BoardFile.model_validate({'format': 'doska', 'version': 1, **bad})


def test_room_state_limits_and_snapshot():
    room = Room('b' * 22, 'T', 'creatorkey', chat_keep=2, saved_at=None)
    a = room.make_stroke(StrokeDTO(id='stroke001', tool='pen', color='#000000', width=2, pts=[0, 0, 1, 1]),
                         'stroke001', 'authorkey1')
    room.add_stroke(a)
    assert room.usage() == {'strokes': 1, 'values': 4, 'chat': 0} and room.dirty
    with pytest.raises(LimitError):
        room.check_capacity(1, 4, max_strokes=1, max_values=100)
    with pytest.raises(LimitError):
        room.check_capacity(0, 97, max_strokes=10, max_values=100)
    assert not room.remove_stroke('stroke001', 'otherauthor') and room.remove_stroke('stroke001', 'authorkey1')
    wire = json.loads(a.wire())
    assert wire == {'id': 'stroke001', 'tool': 'pen', 'color': '#000000', 'width': 2, 'by': author_tag('authorkey1'),
                    'pts': [0, 0, 1, 1]} and 'authorkey1' not in a.wire()

    class P:  # минимальный участник для чата
        author, tag, name, color = 'authorkey1', author_tag('authorkey1'), 'A', '#111111'
    for text in ('1', '2', '3'):
        room.add_chat(P, text)
    assert [m.text for m in room.chat] == ['2', '3']  # хранится только последние chat_keep
    snap = room.snapshot()
    assert snap.strokes == [] and [m.text for m in snap.chat] == ['2', '3']


def test_entry_guard_locks_ip_and_board():
    tracker = FailureTracker(limit=3, window_seconds=60, lock_seconds=30)
    assert tracker.fail('k', now=0) == 0 and tracker.fail('k', now=1) == 0
    assert tracker.fail('k', now=2) == 30 and tracker.retry_after('k', now=10) == 22
    assert tracker.retry_after('k', now=40) == 0

    guard = EntryGuard(handshakes_per_minute=2, ip_failures=3, board_failures=4, window_seconds=60, lock_seconds=60)
    assert guard.allow_handshake('1.1.1.1') and guard.allow_handshake('1.1.1.1')
    assert not guard.allow_handshake('1.1.1.1')
    guard.success('9.9.9.9', 'board')               # этот адрес уже входил на доску
    for ip in ('2.2.2.1', '2.2.2.2', '2.2.2.3', '2.2.2.4'):  # распределённый перебор одной доски
        guard.fail(ip, 'board')
    assert guard.retry_after('5.5.5.5', 'board') > 0      # новые адреса ждут
    assert guard.retry_after('9.9.9.9', 'board') == 0     # знакомый адрес входит
    assert guard.retry_after('5.5.5.5', 'other') == 0
    for _ in range(3):
        guard.fail('7.7.7.7', None)
    assert guard.retry_after('7.7.7.7', 'other') > 0      # перебор с одного адреса по любым доскам


def test_cursor_batching():
    async def scenario():
        room = Room('b' * 22, 'T', None, chat_keep=5, saved_at=None)
        sent = []
        room.broadcast = lambda message, exclude=None: sent.append(message)
        task = asyncio.create_task(room.cursor_loop())
        room.cursors['p1'] = [1, 2]
        room.cursors['p1'] = [3, 4]   # промежуточная позиция заменяется
        room.cursors['p2'] = None
        await asyncio.sleep(0.25)
        task.cancel()
        return sent
    sent = asyncio.run(scenario())
    assert sent == [{'t': 'cursors', 'c': [['p1', 3, 4], ['p2', None, None]]}]
