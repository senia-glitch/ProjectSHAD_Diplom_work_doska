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
from app.scenarios.boards import (BOARD_ID_PATTERN, CHAT_MAX_CHARS, KEY_PATTERN, AdmitDTO, BanDTO, BoardCreated,
                                  BoardFile, ChatTextDTO, CodeDTO, CreateBoardDTO, SetCodeDTO, StrokeDTO, author_tag)

router = APIRouter(tags=['boards'])
log = logging.getLogger('app.board')
_create_limiter = KeyedRateLimiter(cfg.creates_per_minute)
_import_limiter = KeyedRateLimiter(cfg.imports_per_minute)
guard = EntryGuard(cfg.handshakes_per_minute, cfg.ip_code_failures, cfg.board_code_failures,
                   cfg.code_failure_window, cfg.code_lock_seconds, cfg.global_code_failures)
_BOARD_ID = re.compile(BOARD_ID_PATTERN)
_KEY = re.compile(KEY_PATTERN)

# Коды закрытия WebSocket, которые понимает клиент.
CLOSE_BAD_CODE, CLOSE_FORBIDDEN, CLOSE_NOT_FOUND, CLOSE_LOCKED, CLOSE_KICKED, CLOSE_FULL = \
    4401, 4403, 4404, 4408, 4410, 4429
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


class Banned(BaseHTTPError):
    http_status = 403
    default_code = 4032


class CodeNotFound(BaseHTTPError):
    http_status = 404
    default_code = 4041


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


async def admit(ip: str, board_id: str, author: str, code: str):
    """
    Решение о входе с учётом защиты от перебора. Возвращает (причина отказа или None, BoardAdmit).
    Участник, уже входивший на доску, и создатель входят без кода и даже во время блокировки адреса.
    """
    wait = guard.retry_after(ip, board_id)
    result = await run('boards.admit', AdmitDTO(board_id=board_id, author=author, code='' if wait else code))
    if not result.found:
        return 'not_found', result
    if result.banned:
        return 'banned', result
    if result.granted:
        guard.success(ip, board_id)
        return None, result
    if wait:
        return f'locked:{math.ceil(wait)}', result
    if result.code_tried:  # неудачей считается только введённый неверный код
        guard.fail(ip, board_id)
    return 'bad_code', result


@router.post('/boards/resolve', dependencies=[Depends(require('boards.join'))])
async def resolve_board(dto: CodeDTO, request: Request):
    """Вход только по коду: код указывает на доску. Неверные коды считаются защитой от перебора."""
    ip = _client(request)
    wait = guard.retry_after(ip)
    if wait:
        raise TooManyRequests(f'Слишком много неверных кодов. Повторите через {math.ceil(wait / 60)} мин')
    result = await run('boards.resolve', dto)
    if not result.found:
        guard.fail(ip)
        raise CodeNotFound('Доска с таким кодом не найдена')
    guard.success(ip)
    return {'id': result.id, 'title': result.title}


class ImportRequest(BaseModel):
    mode: Literal['append', 'replace'] = 'append'
    author: str = Field(pattern=KEY_PATTERN)
    name: str = Field(default='', max_length=200)
    code: str = Field(default='', max_length=64)
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
    denied, _ = await admit(ip, board_id, req.author, req.code)
    if denied == 'not_found':
        raise NotFoundError('Доска не найдена')
    if denied == 'banned':
        raise Banned('Вы исключены из этой доски')
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
    elif reason == 'banned':
        await ws.close(code=CLOSE_KICKED, reason='banned')
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
        await _deny(ws, 'not_found')
        return
    denied, _ = await admit(ip, board_id, hello.author, hello.code)
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

                elif kind == 'setcode':
                    if not peer.creator:
                        peer.send({'t': 'error', 'message': 'Код меняет только создатель доски'})
                        continue
                    new = await run('boards.set_code', SetCodeDTO(board_id=board_id, code=msg.get('code') or None))
                    peer.send({'t': 'code', 'code': new.code})

                elif kind == 'kick':
                    target = room.peers.get(msg.get('id')) if isinstance(msg.get('id'), str) else None
                    if not peer.creator:
                        peer.send({'t': 'error', 'message': 'Исключать участников может только создатель доски'})
                        continue
                    if target is None or target.creator or target.author == peer.author:
                        peer.send({'t': 'error', 'message': 'Этого участника нельзя исключить'})
                        continue
                    await run('boards.ban', BanDTO(board_id=board_id, author=target.author))
                    new = await run('boards.set_code', SetCodeDTO(board_id=board_id))
                    # Все вкладки исключённого: тот же ключ автора.
                    for other in [p for p in room.peers.values() if p.author == target.author]:
                        await hub.kick(room, other)
                    peer.send({'t': 'code', 'code': new.code, 'kicked': target.name})

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
                peer.send({'t': 'error', 'message': str(exc) or 'Ошибка операции'})
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
