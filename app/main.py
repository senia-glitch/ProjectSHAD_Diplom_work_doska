from app.config import ROOT
from contextlib import asynccontextmanager
from core import ScenarioRegistry, shutdown_core, start_core
from fastapi import Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastbase import make_app, start
from run_infrastructure import start_infrastructure

from app.scenarios.boards import BOARD_ID_PATTERN

REQUIRED_SCENARIOS = {'boards.create', 'boards.get', 'strokes.list', 'strokes.add', 'strokes.remove',
                      'strokes.clear', 'service.ready'}
FRONTEND = ROOT / 'frontend'

SECURITY_HEADERS = [
    (b'x-content-type-options', b'nosniff'),
    # Ссылка на доску — это ключ доступа: не передаём её сторонним сайтам в Referer.
    (b'referrer-policy', b'no-referrer'),
    (b'x-frame-options', b'DENY'),
    (b'content-security-policy',
     b"default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
     b"script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"),
]


class SecurityHeaders:
    """Чистый ASGI-middleware: заголовки для HTTP-ответов, WebSocket не затрагивается."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        async def wrapped(message):
            if message['type'] == 'http.response.start':
                names = {k for k, _ in message.get('headers', [])}
                message['headers'] = list(message.get('headers', [])) + [
                    h for h in SECURITY_HEADERS if h[0] not in names]
            await send(message)
        await self.app(scope, receive, wrapped)


@asynccontextmanager
async def lifespan(app):
    router = await start_infrastructure()
    try:
        await start_core(router=router, discover='app.scenarios')
        if not REQUIRED_SCENARIOS.issubset(set(ScenarioRegistry.list_scenarios())):
            raise RuntimeError('Required scenarios were not discovered')
        yield
    finally:
        try:
            await shutdown_core()
        finally:
            await router.shutdown()


app = make_app()
app.router.lifespan_context = lifespan
app.add_middleware(SecurityHeaders)


@app.get('/b/{board_id}', include_in_schema=False)
async def board_page(board_id: str = Path(pattern=BOARD_ID_PATTERN)):
    # Существование доски проверяет клиент через API/WebSocket и показывает понятную ошибку.
    return FileResponse(FRONTEND / 'board.html', headers={'Cache-Control': 'no-cache'})


# API и страница доски объявлены раньше статики.
app.mount('/', StaticFiles(directory=FRONTEND, html=True), name='frontend')

if __name__ == '__main__':
    start()
