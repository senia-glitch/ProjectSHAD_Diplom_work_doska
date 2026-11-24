import time
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api.v1.routes import boards as routes
from app.config import board_settings
from app.realtime.guard import EntryGuard
from app.realtime.hub import hub


@pytest.fixture(autouse=True)
def relaxed_guard(monkeypatch):
    # Все тестовые подключения идут с одного адреса: защиту входа проверяет отдельный тест.
    monkeypatch.setattr(routes, 'guard', EntryGuard(10000, 1000, 1000, 60, 60))


def key():
    return uuid.uuid4().hex[:20]


def new_board(client, title='Тест', code=None):
    response = client.post('/api/v1/boards', json={'title': title, 'author': key(), 'code': code})
    assert response.status_code == 201, response.text
    return response.json()


def join(client, board, name, author=None, code=None, headers=None):
    ws = client.websocket_connect(f"/api/v1/boards/{board['id']}/ws", headers=headers or {})
    conn = ws.__enter__()
    conn.send_json({'t': 'hello', 'author': author or key(), 'name': name,
                    'code': board['code'] if code is None else code})
    return ws, conn


def recv_until(conn, kind, limit=50):
    for _ in range(limit):
        msg = conn.receive_json()
        if msg['t'] == kind:
            return msg
    raise AssertionError(f'{kind} not received')


def closed_with(client, board, code=None, headers=None, path=None):
    with client.websocket_connect(path or f"/api/v1/boards/{board['id']}/ws", headers=headers or {}) as ws:
        ws.send_json({'t': 'hello', 'author': key(), 'name': 'x', 'code': board['code'] if code is None else code})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    return closed.value


def stroke(tool='pen', pts=(0, 0, 10, 10.04, 20, 5)):
    return {'id': key(), 'tool': tool, 'color': '#112233', 'width': 4, 'pts': list(pts)}


def wait_unloaded(board_id, timeout=5):
    deadline = time.monotonic() + timeout
    while board_id in hub.rooms:
        assert time.monotonic() < deadline, 'board was not unloaded'
        time.sleep(0.02)


def test_service_and_board_creation(client):
    assert client.get('/api/v1/health').json() == {'status': 'ok'}
    assert client.get('/api/v1/ready').json() == {'status': 'ready'}
    board = new_board(client, '  План   спринта ')
    assert board['title'] == 'План спринта' and len(board['id']) >= 16 and len(board['code']) == 8
    assert new_board(client, code='sun-flower 7')['code'] == 'SUNFLOWER7'
    bad = client.post('/api/v1/boards', json={'title': 'x', 'author': key(), 'code': 'ab'})
    assert bad.status_code == 422
    assert client.post('/api/v1/boards', json={'title': 'x' * 121, 'author': key()}).status_code == 422
    assert client.get(f"/api/v1/boards/{board['id']}").status_code in (404, 405)  # без кода доска не читается
    page = client.get(f"/b/{board['id']}")
    assert page.status_code == 200 and 'board.js' in page.text
    assert page.headers['referrer-policy'] == 'no-referrer'
    assert client.get('/docs').status_code == 404


def test_realtime_collaboration_and_persistence(client):
    board = new_board(client)
    alice_key, bob_key = key(), key()
    a_ctx, alice = join(client, board, 'Алиса', alice_key)
    try:
        init_a = recv_until(alice, 'init')
        assert init_a['strokes'] == [] and init_a['peers'] == [] and init_a['you']['creator'] is False
        assert init_a['limits']['maxPeers'] == 15 and init_a['board']['title'] == 'Тест'
        b_ctx, bob = join(client, board, 'Боб', bob_key, code=board['code'].lower())
        try:
            init_b = recv_until(bob, 'init')
            assert [p['name'] for p in init_b['peers']] == ['Алиса']
            assert recv_until(alice, 'join')['peer']['name'] == 'Боб'

            # Живых частей штриха больше нет: штрих приходит целиком после завершения.
            alice.send_json({'t': 'draw', **stroke()})
            assert 'Неизвестное' in recv_until(alice, 'error')['message']
            first = stroke()
            alice.send_json({'t': 'stroke', 'stroke': first})
            saved = recv_until(bob, 'stroke')['stroke']
            assert saved['id'] == first['id'] and saved['pts'][3] == 10.0
            assert saved['by'] == init_a['you']['tag'] and 'author' not in saved  # ключ автора не раскрывается
            assert recv_until(alice, 'stroke')['stroke']['id'] == first['id']
            alice.send_json({'t': 'stroke', 'stroke': first})            # повтор после переподключения
            assert recv_until(alice, 'stroke')['stroke']['id'] == first['id']

            # Чужой штрих отменить нельзя, свой — можно.
            bob.send_json({'t': 'remove', 'id': first['id']})
            second = stroke('rect', (0, 0, 50, 50))
            alice.send_json({'t': 'stroke', 'stroke': second})
            assert recv_until(bob, 'stroke')['stroke']['id'] == second['id']
            alice.send_json({'t': 'remove', 'id': second['id']})
            assert recv_until(bob, 'remove')['id'] == second['id']

            # Курсоры приходят пачкой, промежуточные позиции схлопываются.
            for x in range(5):
                alice.send_json({'t': 'cursor', 'x': x, 'y': 6})
            batch = recv_until(bob, 'cursors')['c']
            assert [init_a['you']['id'], 4, 6] in batch

            alice.send_json({'t': 'chat', 'text': '  привет\r\n\n\n\n\tмир  '})
            msg = recv_until(bob, 'chat')['message']
            assert msg['text'] == 'привет\n\n мир' and msg['tag'] == init_a['you']['tag']

            alice.send_json({'t': 'stroke', 'stroke': {**stroke(), 'pts': [1, 2, 3]}})
            assert recv_until(alice, 'error')
        finally:
            b_ctx.__exit__(None, None, None)
        assert recv_until(alice, 'leave')
    finally:
        a_ctx.__exit__(None, None, None)

    # Последний участник ушёл: доска сохранена и выгружена из памяти; вход поднимает её из БД.
    wait_unloaded(board['id'])
    c_ctx, carol = join(client, board, 'Кэрол', alice_key)
    try:
        init_c = recv_until(carol, 'init')
        assert [s['id'] for s in init_c['strokes']] == [first['id']]
        assert init_c['chat'][0]['text'] == 'привет\n\n мир' and init_c['savedAt']
        carol.send_json({'t': 'remove', 'id': first['id']})  # тот же ключ автора — свой штрих
        assert recv_until(carol, 'remove')
        carol.send_json({'t': 'clear'})
        assert recv_until(carol, 'clear')['by'] == 'Кэрол'
    finally:
        c_ctx.__exit__(None, None, None)
    wait_unloaded(board['id'])
    d_ctx, dave = join(client, board, 'Дэйв')
    try:
        assert recv_until(dave, 'init')['strokes'] == []
    finally:
        d_ctx.__exit__(None, None, None)


def test_autosave_and_housekeeping(client):
    from app.scenarios.boards import CleanupDTO, TouchDTO
    from core import run

    board = new_board(client)
    ctx, alice = join(client, board, 'Алиса')
    try:
        recv_until(alice, 'init')
        alice.send_json({'t': 'stroke', 'stroke': stroke()})
        recv_until(alice, 'stroke')
        room = hub.rooms[board['id']]
        assert room.dirty
        assert client.portal.call(hub.save, room) is True      # то же делает автосохранение раз в N минут
        assert recv_until(alice, 'saved')['at'] and not room.dirty
        assert client.portal.call(hub.save, room) is False     # без изменений не пишет
        assert client.portal.call(run, 'boards.touch', TouchDTO(board_ids=[board['id']])).count == 1
        assert client.portal.call(run, 'boards.cleanup', CleanupDTO(days=1)).count == 0
    finally:
        ctx.__exit__(None, None, None)


def test_entry_protection(client, monkeypatch):
    board = new_board(client, code='secret42')
    assert closed_with(client, board, code='wrong').code == 4401
    assert closed_with(client, {'id': 'y' * 22, 'code': 'x'}).code == 4404
    assert closed_with(client, board, path=f"/api/v1/boards/bad.id/ws").code == 4404
    with pytest.raises(WebSocketDisconnect) as foreign:
        with client.websocket_connect(f"/api/v1/boards/{board['id']}/ws", headers={'origin': 'https://evil.example'}):
            pass
    assert foreign.value.code == 4403
    with client.websocket_connect(f"/api/v1/boards/{board['id']}/ws") as ws:
        ws.send_json({'t': 'hello', 'author': 'short', 'name': 'x'})
        with pytest.raises(WebSocketDisconnect) as bad:
            ws.receive_json()
        assert bad.value.code == 1008

    # Перебор кода: после лимита неудач даже верный код отклоняется до конца блокировки.
    monkeypatch.setattr(routes, 'guard', EntryGuard(10000, 3, 1000, 60, 60))
    for _ in range(3):
        assert closed_with(client, board, code='guess').code == 4401
    locked = closed_with(client, board)
    assert locked.code == 4408 and locked.reason.startswith('retry:')

    # Спам подключениями.
    monkeypatch.setattr(routes, 'guard', EntryGuard(2, 1000, 1000, 60, 60))
    for _ in range(2):
        ctx, conn = join(client, board, 'x')
        recv_until(conn, 'init')
        ctx.__exit__(None, None, None)
    assert closed_with(client, board).code == 4408

    # Не больше max_peers участников.
    monkeypatch.setattr(routes, 'guard', EntryGuard(10000, 1000, 1000, 60, 60))
    monkeypatch.setattr(board_settings, 'max_peers', 1)
    ctx, conn = join(client, board, 'первый')
    try:
        recv_until(conn, 'init')
        assert closed_with(client, board).code == 4429
    finally:
        ctx.__exit__(None, None, None)


def test_import(client, monkeypatch):
    data = {'format': 'doska', 'version': 1, 'title': 'Схема',
            'strokes': [{'tool': 'pen', 'color': '#112233', 'width': 4, 'pts': [0, 0, 10, 10]},
                        {'tool': 'ellipse', 'color': '#112233', 'width': 4, 'pts': [0, 0, 30, 20]}]}
    target = new_board(client)
    author = key()
    url = f"/api/v1/boards/{target['id']}/import"
    body = lambda **kw: {'author': author, 'code': target['code'], 'board': data, **kw}

    # Доска без участников: загрузка поднимает её из БД, применяет и сохраняет.
    r = client.post(url, json=body(mode='append', name='Импорт'))
    assert r.status_code == 200 and r.json() == {'imported': 2, 'replace': False}
    wait_unloaded(target['id'])

    ctx, bob = join(client, target, 'Боб')
    try:
        assert len(recv_until(bob, 'init')['strokes']) == 2
        assert client.post(url, json=body(mode='append', name='Импорт')).json()['imported'] == 2
        pushed = recv_until(bob, 'import')
        assert pushed['replace'] is False and pushed['by'] == 'Импорт' and len(pushed['strokes']) == 2
        assert pushed['strokes'][1]['tool'] == 'ellipse' and 'author' not in pushed['strokes'][0]
        assert client.post(url, json=body(mode='replace')).json() == {'imported': 2, 'replace': True}
        assert recv_until(bob, 'import')['replace'] is True
        assert len(hub.rooms[target['id']].strokes) == 2

        # Ошибки не меняют доску.
        assert client.post(url, json=body(code='wrong')).status_code == 403
        broken = {**data, 'strokes': [{**data['strokes'][0], 'pts': [1, 2, 3]}]}
        assert client.post(url, json=body(board=broken)).status_code == 422
        assert client.post(url, json=body(board={**data, 'format': 'other'})).status_code == 422
        assert client.post(url, content=b'not json').status_code == 422
        monkeypatch.setattr(board_settings, 'max_strokes', 3)
        assert client.post(url, json=body()).status_code == 409
        monkeypatch.setattr(board_settings, 'max_import_bytes', 1024)
        big = {**data, 'strokes': data['strokes'] * 20}
        assert client.post(url, json=body(mode='replace', board=big)).status_code == 413
        assert len(hub.rooms[target['id']].strokes) == 2
    finally:
        ctx.__exit__(None, None, None)
    missing = '/api/v1/boards/' + 'z' * 22 + '/import'
    assert client.post(missing, json=body()).status_code == 404
