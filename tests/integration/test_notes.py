def test_note_roundtrip_and_validation(client):
    assert client.get('/api/v1/health').json() == {'status': 'ok'}
    assert client.post('/api/v1/notes', json={'title': ''}).status_code == 422
    created = client.post('/api/v1/notes', json={'title': 'Integration example'})
    assert created.status_code == 201
    note = created.json()
    try:
        assert client.get(f"/api/v1/notes/{note['id']}").json() == note
        page = client.get('/api/v1/notes', params={'limit': 100}).json()
        assert page['total'] >= 1
        assert all(a['id'] < b['id'] for a, b in zip(page['items'], page['items'][1:]))
        assert client.get('/api/v1/notes', params={'limit': 101}).status_code == 422
    finally:
        assert client.delete(f"/api/v1/notes/{note['id']}").status_code == 204
    assert client.get(f"/api/v1/notes/{note['id']}").status_code == 404
