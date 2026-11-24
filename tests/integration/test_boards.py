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


def test_chat_history_and_limits(client):
    bid = new_board(client)['id']
    a_ctx, alice = join(client, bid, 'Алиса')
    try:
        me = recv_until(alice, 'init')
        assert me['chat'] == [] and len(me['you']['tag']) == 12
        b_ctx, bob = join(client, bid, 'Боб')
        try:
            recv_until(bob, 'init')
            alice.send_json({'t': 'chat', 'text': '  привет\r\n\n\n\n\tмир  '})
            got = recv_until(bob, 'chat')['message']
            assert got['text'] == 'привет\n\n мир' and got['name'] == 'Алиса' and got['tag'] == me['you']['tag']
            assert 'author' not in got
            assert recv_until(alice, 'chat')['message']['id'] == got['id']  # эхо автору
            for bad in ('   ', 'x' * 1001, 123):
                alice.send_json({'t': 'chat', 'text': bad})
                assert recv_until(alice, 'error')
            for i in range(8):  # серия сверх лимита отклоняется, соединение живо
                bob.send_json({'t': 'chat', 'text': f'm{i}'})
            assert 'Слишком часто' in recv_until(bob, 'error')['message']
            bob.send_json({'t': 'ping'})
            assert recv_until(bob, 'pong')
        finally:
            b_ctx.__exit__(None, None, None)
    finally:
        a_ctx.__exit__(None, None, None)
    c_ctx, carol = join(client, bid, 'Кэрол')
    try:
        history = recv_until(carol, 'init')['chat']
        assert history[0]['text'] == 'привет\n\n мир' and history[1]['name'] == 'Боб'
        assert [m['id'] for m in history] == sorted(m['id'] for m in history)
    finally:
        c_ctx.__exit__(None, None, None)


def test_export_and_import(client, monkeypatch):
    from app.config import board_settings

    source = new_board(client, 'Схема / v1')['id']
    ctx, alice = join(client, source, 'Алиса')
    try:
        recv_until(alice, 'init')
        for s in (stroke(), stroke('ellipse', (0, 0, 30, 20))):
            alice.send_json({'t': 'stroke', 'stroke': s})
            recv_until(alice, 'stroke')
    finally:
        ctx.__exit__(None, None, None)

    exported = client.get(f'/api/v1/boards/{source}/export')
    assert exported.status_code == 200
    assert "filename*=UTF-8''" in exported.headers['content-disposition']
    data = exported.json()
    assert data['format'] == 'doska' and data['version'] == 1 and data['title'] == 'Схема / v1'
    assert [s['tool'] for s in data['strokes']] == ['pen', 'ellipse'] and 'id' not in data['strokes'][0]

    target = new_board(client)['id']
    author = key()
    url = f'/api/v1/boards/{target}/import'
    ctx, bob = join(client, target, 'Боб')
    try:
        recv_until(bob, 'init')
        bob.send_json({'t': 'stroke', 'stroke': stroke('rect', (5, 5, 9, 9))})
        recv_until(bob, 'stroke')

        r = client.post(url, json={'mode': 'append', 'author': author, 'name': 'Импорт', 'board': data})
        assert r.status_code == 200 and r.json() == {'imported': 2, 'replace': False}
        pushed = recv_until(bob, 'import')
        assert pushed['replace'] is False and pushed['by'] == 'Импорт' and len(pushed['strokes']) == 2
        assert pushed['strokes'][0]['author'] == author and pushed['strokes'][1]['tool'] == 'ellipse'
        assert len(client.get(f'/api/v1/boards/{target}/export').json()['strokes']) == 3

        # Повторная загрузка того же файла: ключи новые, конфликтов нет; замена убирает старое.
        r = client.post(url, json={'mode': 'replace', 'author': author, 'board': data})
        assert r.json() == {'imported': 2, 'replace': True}
        assert recv_until(bob, 'import')['replace'] is True
        assert len(client.get(f'/api/v1/boards/{target}/export').json()['strokes']) == 2

        # Ошибки не меняют доску.
        broken = {**data, 'strokes': [{**data['strokes'][0], 'pts': [1, 2, 3]}]}
        assert client.post(url, json={'author': author, 'board': broken}).status_code == 422
        assert client.post(url, json={'author': author, 'board': {**data, 'format': 'other'}}).status_code == 422
        assert client.post(url, content=b'not json').status_code == 422
        monkeypatch.setattr(board_settings, 'max_strokes', 3)
        assert client.post(url, json={'author': author, 'board': data}).status_code == 409
        monkeypatch.setattr(board_settings, 'max_import_bytes', 1024)
        big = {**data, 'strokes': data['strokes'] * 20}
        assert client.post(url, json={'mode': 'replace', 'author': author, 'board': big}).status_code == 413
        assert len(client.get(f'/api/v1/boards/{target}/export').json()['strokes']) == 2
        bob.send_json({'t': 'ping'})
        assert recv_until(bob, 'pong')
    finally:
        ctx.__exit__(None, None, None)
    missing = '/api/v1/boards/' + 'z' * 22
    assert client.post(missing + '/import', json={'author': author, 'board': data}).status_code == 404
    assert client.get(missing + '/export').status_code == 404
