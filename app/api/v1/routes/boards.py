"""Внешние сценарии доски: создание, вход по ссылке и коду, совместная работа через WebSocket, загрузка файла."""
import asyncio
import json
import logging
import math
import re
import secrets
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Path, Request, WebSocket, WebSocketDisconnect
from fastbase import BaseHTTPError
from pydantic import BaseModel, Field, ValidationError, field_validator
from core import CoreError, NotFoundError, run

from app.access import is_allowed, require, role_of
from app.config import board_settings as cfg
from app.realtime.guard import EntryGuard
from app.realtime.hub import KeyedRateLimiter, LimitError, Peer, RateLimiter, SlowConsumer, hub
from app.scenarios.boards import (BOARD_ID_PATTERN, CHAT_MAX_CHARS, KEY_PATTERN, AccessDTO, BoardCreated, BoardFile,
                                  ChatTextDTO, CreateBoardDTO, StrokeDTO, author_tag)

router = APIRouter(tags=['boards'])
log = logging.getLogger('app.board')
_create_limiter = KeyedRateLimiter(cfg.creates_per_minute)
_import_limiter = KeyedRateLimiter(cfg.imports_per_minute)
guard = EntryGuard(cfg.handshakes_per_minute, cfg.ip_code_failures, cfg.board_code_failures,
                   cfg.code_failure_window, cfg.code_lock_seconds)
_BOARD_ID = re.compile(BOARD_ID_PATTERN)
_KEY = re.compile(KEY_PATTERN)

# Коды закрытия WebSocket, которые понимает клиент.
CLOSE_BAD_CODE, CLOSE_FORBIDDEN, CLOSE_NOT_FOUND, CLOSE_LOCKED, CLOSE_FULL = 4401, 4403, 4404, 4408, 4429
CLOSE_POLICY, CLOSE_TOO_BIG = 1008, 1009


class TooManyRequests(BaseHTTPError):
    http_status = 429
    default_code = 4290


class PayloadTooLarge(BaseHTTPError):
    http_status = 413
    default_code = 4130


class BadBoardFile(BaseHTTPError):
    http_status = 422
    default_code = 4221


class WrongCode(BaseHTTPError):
    http_status = 403
    default_code = 4031


class BoardFull(BaseHTTPError):
    http_status = 409
    default_code = 4091


def _client(conn: Request | WebSocket) -> str:
    return conn.client.host if conn.client else 'unknown'


def limits() -> dict:
    return {'maxStrokes': cfg.max_strokes, 'maxValues': cfg.max_values, 'maxPeers': cfg.max_peers,
            'chatKeep': cfg.chat_keep, 'chatMaxChars': CHAT_MAX_CHARS, 'autosaveMinutes': cfg.autosave_minutes}


def clean_name(value: str) -> str:
    value = ' '.join(''.join(ch for ch in value if ch.isprintable()).split())[:32]
    return value or 'Гость'


# ---------------------------------------------------------------- HTTP

@router.post('/boards', status_code=201, response_model=BoardCreated,
             dependencies=[Depends(require('boards.create'))])
async def create_board(dto: CreateBoardDTO, request: Request):
    """Создатель получает код входа (сгенерированный или свой) только в этом ответе: сервер хранит лишь хеш."""
    if not _create_limiter.allow(_client(request)):
        raise TooManyRequests('Слишком много новых досок, попробуйте через минуту')
    return await run('boards.create', dto)


async def check_code(ip: str, board_id: str, code: str) -> str | None:
    """Проверка кода с учётом защиты от перебора. Возвращает причину отказа или None."""
    wait = guard.retry_after(ip, board_id)
    if wait:
        return f'locked:{math.ceil(wait)}'
    access = await run('boards.access', AccessDTO(board_id=board_id, code=code))
    if not access.found:
        guard.fail(ip, None)
        return 'not_found'
    if not access.granted:
        guard.fail(ip, board_id)
        return 'bad_code'
    guard.success(ip, board_id)
    return None


class ImportRequest(BaseModel):
    mode: Literal['append', 'replace'] = 'append'
    author: str = Field(pattern=KEY_PATTERN)
    name: str = Field(default='', max_length=200)
    code: str = Field(max_length=64)
    board: BoardFile


async def _read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get('content-length')
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLarge(f'Файл больше {limit // (1024 * 1024)} МБ')
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise PayloadTooLarge(f'Файл больше {limit // (1024 * 1024)} МБ')
    return bytes(body)


@router.post('/boards/{board_id}/import', dependencies=[Depends(require('boards.import'))])
async def import_board(request: Request, board_id: str = Path(pattern=BOARD_ID_PATTERN)):
    """Файл доски: добавить к содержимому или заменить его. Участники видят результат сразу."""
    ip = _client(request)
    if not _import_limiter.allow(ip):
        raise TooManyRequests('Слишком много загрузок, попробуйте через минуту')
    body = await _read_limited(request, cfg.max_import_bytes)
    try:
        req = ImportRequest.model_validate_json(body)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = '.'.join(str(p) for p in first.get('loc', ()))
        raise BadBoardFile(f'Файл доски не распознан ({where or "формат"})')
    denied = await check_code(ip, board_id, req.code)
    if denied == 'not_found':
        raise NotFoundError('Доска не найдена')
    if denied == 'bad_code':
        raise WrongCode('Неверный код доски')
    if denied:
        raise TooManyRequests('Слишком много неверных кодов, попробуйте позже')

    room = await hub.open(board_id)
    try:
        replace = req.mode == 'replace'
        records = [room.make_stroke(s, secrets.token_urlsafe(15), req.author) for s in req.board.strokes]
        values = sum(r.values for r in records)
        if replace:
            if len(records) > cfg.max_strokes or values > cfg.max_values:
                raise BoardFull('Файл не помещается в лимит доски')
            room.clear()
        else:
            try:
                room.check_capacity(len(records), values, cfg.max_strokes, cfg.max_values)
            except LimitError:
                raise BoardFull('Файл не помещается на доску: очистите её или замените содержимое')
        for rec in records:
            room.add_stroke(rec)
        # Без await между изменением и рассылкой: порядок событий у всех одинаковый.
        room.broadcast('{"t":"import","replace":%s,"by":%s,"strokes":[%s]}' % (
            'true' if replace else 'false', json.dumps(clean_name(req.name), ensure_ascii=False),
            ','.join(r.wire() for r in records)))
    finally:
        hub.release(room)
    return {'imported': len(records), 'replace': replace}


# ---------------------------------------------------------------- WebSocket

class HelloMsg(BaseModel):
    t: Literal['hello']
    author: str = Field(pattern=KEY_PATTERN)
    name: str = Field(default='', max_length=200)
    code: str = Field(default='', max_length=64)

    @field_validator('name')
    @classmethod
    def _name(cls, value):
        return clean_name(value)


def origin_allowed(ws: WebSocket) -> bool:
    origin = ws.headers.get('origin')
    if origin is None:  # не браузер (скрипт, тест): cookie-сессий нет, подделывать нечего
        return True
    origin = origin.rstrip('/').lower()
    if cfg.allowed_origins:
        return origin in cfg.allowed_origins
    return urlsplit(origin).netloc == ws.headers.get('host', '').lower()


async def _sender(peer: Peer):
    try:
        while True:
            await peer.ws.send_text(await peer.queue.get())
    except Exception:
        peer.closed = True
        try:
            await peer.ws.close(code=1011)
        except Exception:
            pass


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
        and abs(value) <= 1_000_000


async def _deny(ws: WebSocket, reason: str):
    if reason.startswith('locked:'):
        await ws.close(code=CLOSE_LOCKED, reason='retry:' + reason.split(':', 1)[1])
    elif reason == 'not_found':
        await ws.close(code=CLOSE_NOT_FOUND, reason='board not found')
    else:
        await ws.close(code=CLOSE_BAD_CODE, reason='bad code')


@router.websocket('/boards/{board_id}/ws')
async def board_socket(ws: WebSocket, board_id: str):
    if not origin_allowed(ws) or not is_allowed(role_of(ws), 'boards.join'):
        await ws.close(code=CLOSE_FORBIDDEN)  # до accept: клиент получает отказ рукопожатия
        return
    # Браузер видит свой код закрытия только после accept; до accept он получает лишь 1006.
    await ws.accept()
    ip = _client(ws)
    if not guard.allow_handshake(ip):
        await ws.close(code=CLOSE_LOCKED, reason='retry:60')
        return
    try:
        hello = HelloMsg.model_validate_json(await asyncio.wait_for(ws.receive_text(), timeout=10))
    except (asyncio.TimeoutError, ValidationError, WebSocketDisconnect, ValueError):
        await ws.close(code=CLOSE_POLICY, reason='hello expected')
        return
    if not _BOARD_ID.match(board_id):
        guard.fail(ip, None)
        await _deny(ws, 'not_found')
        return
    denied = await check_code(ip, board_id, hello.code)
    if denied:
        await _deny(ws, denied)
        return

    room = await hub.open(board_id)
    try:
        if len(room.peers) >= cfg.max_peers:
            await ws.close(code=CLOSE_FULL, reason='board is full')
            return
        peer = Peer(ws=ws, author=hello.author, tag=author_tag(hello.author), name=hello.name,
                    color=room.next_color(), queue=asyncio.Queue(maxsize=cfg.send_queue),
                    creator=room.creator_key == hello.author)
        hub.join(room, peer)
        peer.send(room.init_message(peer, limits()))
    finally:
        hub.release(room)
    sender = asyncio.create_task(_sender(peer))
    limiter = RateLimiter(cfg.messages_per_second, cfg.messages_per_second * 4)
    chat_limiter = RateLimiter(1, 5)  # чат: в среднем сообщение в секунду, короткая серия до пяти

    # Все изменения доски синхронны (память процесса): между изменением и рассылкой нет await,
    # поэтому порядок событий у всех участников одинаковый без блокировок.
    try:
        while not peer.closed:
            text = await ws.receive_text()
            if len(text) > cfg.max_message_bytes:
                await ws.close(code=CLOSE_TOO_BIG, reason='message too big')
                break
            if not limiter.allow():
                await ws.close(code=CLOSE_POLICY, reason='rate limit')
                break
            try:
                msg = json.loads(text)
                kind = msg.get('t') if isinstance(msg, dict) else None
            except ValueError:
                kind = None

            try:
                if kind == 'cursor':
                    x, y = msg.get('x'), msg.get('y')
                    if x is None:
                        room.cursors[peer.id] = None
                    elif _finite(x) and _finite(y):
                        room.cursors[peer.id] = [round(x, 1), round(y, 1)]

                elif kind == 'stroke':
                    stroke = StrokeDTO.model_validate(msg.get('stroke'))
                    existing = room.strokes.get(stroke.id)
                    if existing is not None:
                        if existing.author == peer.author:  # повтор после переподключения: подтверждаем
                            peer.send('{"t":"stroke","from":"%s","stroke":%s}' % (peer.id, existing.wire()))
                        continue
                    rec = room.make_stroke(stroke, stroke.id, peer.author)
                    try:
                        room.check_capacity(1, rec.values, cfg.max_strokes, cfg.max_values)
                    except LimitError as exc:
                        peer.send({'t': 'reject', 'id': stroke.id, 'message': str(exc)})
                        continue
                    room.add_stroke(rec)
                    room.broadcast('{"t":"stroke","from":"%s","stroke":%s}' % (peer.id, rec.wire()))

                elif kind == 'remove':
                    key = msg.get('id')
                    if isinstance(key, str) and _KEY.match(key) and room.remove_stroke(key, peer.author):
                        room.broadcast({'t': 'remove', 'id': key, 'from': peer.id})

                elif kind == 'clear':
                    room.clear()
                    room.broadcast({'t': 'clear', 'by': peer.name, 'from': peer.id})

                elif kind == 'chat':
                    if not chat_limiter.allow():
                        peer.send({'t': 'error', 'message': 'Слишком часто: подождите секунду'})
                        continue
                    text_dto = ChatTextDTO(text=msg.get('text'))
                    rec = room.add_chat(peer, text_dto.text)
                    room.broadcast({'t': 'chat', 'message': rec.public()})

                elif kind == 'rename':
                    peer.name = clean_name(str(msg.get('name', '')))
                    room.broadcast({'t': 'peer', 'peer': peer.public()})

                elif kind == 'ping':
                    peer.send({'t': 'pong'})

                else:
                    peer.send({'t': 'error', 'message': 'Неизвестное сообщение'})
            except ValidationError:
                peer.send({'t': 'error', 'message': 'Некорректные данные'})
            except CoreError as exc:
                peer.send({'t': 'error', 'message': exc.message or 'Ошибка операции'})
            except (WebSocketDisconnect, SlowConsumer):
                raise
            except Exception:
                log.exception('board message failed')
                peer.send({'t': 'error', 'message': 'Операция не выполнена, повторите позже'})
    except (WebSocketDisconnect, SlowConsumer):
        pass
    except Exception:
        log.exception('board socket failed')
        try:
            await ws.close(code=1011)
        except Exception:
            pass
    finally:
        peer.closed = True
        sender.cancel()
        hub.leave(room, peer)
