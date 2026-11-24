import asyncio
import logging

from app.config import ROOT
from contextlib import asynccontextmanager
from core import ScenarioRegistry, run, shutdown_core, start_core
from fastapi import Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastbase import make_app, start
from run_infrastructure import start_infrastructure

from app.config import board_settings
from app.realtime.hub import hub
from app.scenarios.boards import BOARD_ID_PATTERN, CleanupDTO

log = logging.getLogger('app.main')

REQUIRED_SCENARIOS = {'boards.create', 'boards.access', 'boards.load', 'boards.save', 'boards.touch',
                      'boards.cleanup', 'service.ready'}
CLEANUP_EVERY_SECONDS = 3600
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


async def _cleanup_loop():
    """Удаление досок, на которые не заходили BOARD_RETENTION_DAYS дней."""
    while True:
        try:
            removed = await run('boards.cleanup', CleanupDTO(days=board_settings.retention_days))
            if removed.count:
                log.info('removed %s abandoned boards', removed.count)
        except Exception:
            log.exception('cleanup failed')
        await asyncio.sleep(CLEANUP_EVERY_SECONDS)


@asynccontextmanager
async def lifespan(app):
    router = await start_infrastructure()
    try:
        await start_core(router=router, discover='app.scenarios')
        if not REQUIRED_SCENARIOS.issubset(set(ScenarioRegistry.list_scenarios())):
            raise RuntimeError('Required scenarios were not discovered')
        tasks = [asyncio.create_task(hub.autosave_loop(board_settings.autosave_minutes * 60)),
                 asyncio.create_task(_cleanup_loop())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await hub.shutdown()  # сохранить открытые доски до остановки ядра и БД
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
