import uuid

import pytest
from starlette.websockets import WebSocketDisconnect


def key():
    return uuid.uuid4().hex[:20]


def new_board(client, title='Тест'):
    response = client.post('/api/v1/boards', json={'title': title})
    assert response.status_code == 201, response.text
    return response.json()


def join(client, board_id, name, author=None, headers=None):
    ws = client.websocket_connect(f'/api/v1/boards/{board_id}/ws', headers=headers or {})
    conn = ws.__enter__()
    conn.send_json({'t': 'hello', 'author': author or key(), 'name': name})
    return ws, conn


def recv_until(conn, kind, limit=50):
    for _ in range(limit):
        msg = conn.receive_json()
        if msg['t'] == kind:
            return msg
    raise AssertionError(f'{kind} not received')


def stroke(tool='pen', pts=(0, 0, 10, 10.04, 20, 5)):
    return {'id': key(), 'tool': tool, 'color': '#112233', 'width': 4, 'pts': list(pts)}


def test_service_and_board_http(client):
    assert client.get('/api/v1/health').json() == {'status': 'ok'}
    assert client.get('/api/v1/ready').json() == {'status': 'ready'}
    board = new_board(client, '  План   спринта ')
    assert board['title'] == 'План спринта' and len(board['id']) >= 16
    assert client.get(f"/api/v1/boards/{board['id']}").json()['id'] == board['id']
    assert client.get('/api/v1/boards/' + 'x' * 22).status_code == 404
    assert client.get('/api/v1/boards/bad!id').status_code == 422
    assert client.post('/api/v1/boards', json={'title': 'x' * 121}).status_code == 422
    page = client.get(f"/b/{board['id']}")
    assert page.status_code == 200 and 'board.js' in page.text
    assert page.headers['referrer-policy'] == 'no-referrer'
    assert client.get('/docs').status_code == 404


def test_realtime_collaboration_and_persistence(client):
    board = new_board(client)
    bid = board['id']
    alice_key, bob_key = key(), key()
    a_ctx, alice = join(client, bid, 'Алиса', alice_key)
    try:
        init_a = recv_until(alice, 'init')
        assert init_a['strokes'] == [] and init_a['peers'] == [] and init_a['you']['name'] == 'Алиса'
        b_ctx, bob = join(client, bid, 'Боб', bob_key)
        try:
            init_b = recv_until(bob, 'init')
            assert [p['name'] for p in init_b['peers']] == ['Алиса']
            assert recv_until(alice, 'join')['peer']['name'] == 'Боб'

            # Живой штрих ретранслируется без сохранения.
            live = stroke()
            alice.send_json({'t': 'draw', **live})
            got = recv_until(bob, 'draw')
            assert got['id'] == live['id'] and got['from'] == init_a['you']['id']

            # Законченный штрих сохраняется и приходит всем, включая автора (подтверждение).
            alice.send_json({'t': 'stroke', 'stroke': live})
            saved = recv_until(bob, 'stroke')['stroke']
            assert saved['id'] == live['id'] and saved['author'] == alice_key and saved['pts'][3] == 10.0
            assert recv_until(alice, 'stroke')['stroke']['id'] == live['id']

            # Чужой штрих отменить нельзя, свой — можно.
            bob.send_json({'t': 'remove', 'id': live['id']})
            bob.send_json({'t': 'ping'})
            assert recv_until(bob, 'pong')
            second = stroke('rect', (0, 0, 50, 50))
            alice.send_json({'t': 'stroke', 'stroke': second})
            recv_until(bob, 'stroke')
            alice.send_json({'t': 'remove', 'id': second['id']})
            assert recv_until(bob, 'remove')['id'] == second['id']

            # Некорректные данные не рвут соединение.
            alice.send_json({'t': 'stroke', 'stroke': {**stroke(), 'pts': [1, 2, 3]}})
            assert recv_until(alice, 'error')
            alice.send_json({'t': 'cursor', 'x': 5, 'y': 6})
            assert recv_until(bob, 'cursor')['x'] == 5
        finally:
            b_ctx.__exit__(None, None, None)
        assert recv_until(alice, 'leave')
    finally:
        a_ctx.__exit__(None, None, None)

    # Новый участник после переподключения видит сохранённое состояние.
    c_ctx, carol = join(client, bid, 'Кэрол')
    try:
        init_c = recv_until(carol, 'init')
        assert [s['id'] for s in init_c['strokes']] == [live['id']]
        carol.send_json({'t': 'clear'})
        assert recv_until(carol, 'clear')['by'] == 'Кэрол'
    finally:
        c_ctx.__exit__(None, None, None)
    d_ctx, dave = join(client, bid, 'Дэйв')
    try:
        assert recv_until(dave, 'init')['strokes'] == []
    finally:
        d_ctx.__exit__(None, None, None)


def test_socket_rejections(client):
    for path in ('y' * 22, 'bad.id'):
        with client.websocket_connect(f'/api/v1/boards/{path}/ws') as ws:
            with pytest.raises(WebSocketDisconnect) as missing:
                ws.receive_json()
        assert missing.value.code == 4404
    bid = new_board(client)['id']
    with pytest.raises(WebSocketDisconnect) as foreign:
        with client.websocket_connect(f'/api/v1/boards/{bid}/ws', headers={'origin': 'https://evil.example'}):
            pass
    assert foreign.value.code == 4403
    ctx, conn = join(client, bid, 'Same', headers={'origin': 'http://testserver'})
    try:
        assert recv_until(conn, 'init')
    finally:
        ctx.__exit__(None, None, None)
    with client.websocket_connect(f'/api/v1/boards/{bid}/ws') as ws:
        ws.send_json({'t': 'hello', 'author': 'short', 'name': 'x'})
        with pytest.raises(WebSocketDisconnect) as bad:
            ws.receive_json()
        assert bad.value.code == 1008
