"""Внешние сценарии доски: создание, просмотр и совместное рисование через WebSocket."""
import asyncio
import json
import logging
import math
import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Path, Request, WebSocket, WebSocketDisconnect
from fastbase import BaseHTTPError
from pydantic import BaseModel, Field, ValidationError, field_validator
from core import ConflictError, CoreError, NotFoundError, run

from app.access import is_allowed, require, role_of
from app.config import board_settings as cfg
from app.realtime.hub import KeyedRateLimiter, Peer, RateLimiter, SlowConsumer, hub
from app.scenarios.boards import (BOARD_ID_PATTERN, KEY_PATTERN, AddStrokeDTO, BoardIdDTO, BoardResponse,
                                  CreateBoardDTO, RemoveStrokeDTO, StrokeDTO, Tool)

router = APIRouter(tags=['boards'])
log = logging.getLogger('app.board')
_create_limiter = KeyedRateLimiter(cfg.creates_per_minute)
_BOARD_ID = re.compile(BOARD_ID_PATTERN)

# Коды закрытия WebSocket, которые понимает клиент.
CLOSE_NOT_FOUND, CLOSE_FULL, CLOSE_FORBIDDEN, CLOSE_POLICY, CLOSE_TOO_BIG = 4404, 4429, 4403, 1008, 1009


class TooManyRequests(BaseHTTPError):
    http_status = 429
    default_code = 4290


@router.post('/boards', status_code=201, response_model=BoardResponse,
             dependencies=[Depends(require('boards.create'))])
async def create_board(dto: CreateBoardDTO, request: Request):
    client = request.client.host if request.client else 'unknown'
    if not _create_limiter.allow(client):
        raise TooManyRequests('Слишком много новых досок, попробуйте через минуту')
    return await run('boards.create', dto)


@router.get('/boards/{board_id}', response_model=BoardResponse,
            dependencies=[Depends(require('boards.view'))])
async def get_board(board_id: str = Path(pattern=BOARD_ID_PATTERN)):
    return await run('boards.get', BoardIdDTO(board_id=board_id))


# ---------------------------------------------------------------- WebSocket

class HelloMsg(BaseModel):
    t: Literal['hello']
    author: str = Field(pattern=KEY_PATTERN)
    name: str = Field(default='', max_length=200)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        value = ' '.join(''.join(ch for ch in value if ch.isprintable()).split())[:32]
        return value or 'Гость'


class DrawMsg(BaseModel):
    """Промежуточная часть штриха для живого показа другим участникам; не сохраняется."""
    id: str = Field(pattern=KEY_PATTERN)
    tool: Tool
    color: str = Field(pattern=r'^#[0-9a-fA-F]{6}$')
    width: int = Field(ge=1, le=96)
    pts: list[float] = Field(max_length=4000)
    replace: bool = False

    @field_validator('pts')
    @classmethod
    def check_points(cls, value):
        if len(value) % 2 or not all(math.isfinite(v) and abs(v) <= 1_000_000 for v in value):
            raise ValueError('bad points')
        return value


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


@router.websocket('/boards/{board_id}/ws')
async def board_socket(ws: WebSocket, board_id: str):
    if not origin_allowed(ws) or not is_allowed(role_of(ws), 'boards.collaborate'):
        await ws.close(code=CLOSE_FORBIDDEN)  # до accept: клиент получает отказ рукопожатия
        return
    board = None
    if _BOARD_ID.match(board_id):
        try:
            board = await run('boards.get', BoardIdDTO(board_id=board_id))
        except NotFoundError:
            pass

    # Браузер видит свой код закрытия только после accept; до accept он получает лишь 1006.
    await ws.accept()
    if board is None:
        await ws.close(code=CLOSE_NOT_FOUND, reason='board not found')
        return
    try:
        hello = HelloMsg.model_validate_json(await asyncio.wait_for(ws.receive_text(), timeout=10))
    except (asyncio.TimeoutError, ValidationError, WebSocketDisconnect, ValueError):
        await ws.close(code=CLOSE_POLICY, reason='hello expected')
        return

    room = hub.room(board_id)
    if len(room.peers) >= cfg.max_peers:
        await ws.close(code=CLOSE_FULL, reason='board is full')
        return

    # Регистрация синхронная: до первого await участник уже в комнате, комната не исчезнет.
    peer = Peer(ws=ws, author=hello.author, name=hello.name, color=room.next_color(),
                queue=asyncio.Queue(maxsize=cfg.send_queue))
    room.peers[peer.id] = peer
    room.broadcast({'t': 'join', 'peer': peer.public()}, exclude=peer)
    sender = asyncio.create_task(_sender(peer))
    limiter = RateLimiter(cfg.messages_per_second, cfg.messages_per_second * 4)

    async def mutate(action):
        """Изменения доски выполняются по одному: БД и рассылка в едином порядке."""
        async with room.lock:
            await action()

    try:
        async def send_init():
            strokes = await run('strokes.list', BoardIdDTO(board_id=board_id))
            peer.send({'t': 'init', 'you': peer.public(), 'board': board.model_dump(mode='json'),
                       'peers': [p.public() for p in room.peers.values() if p is not peer],
                       'strokes': [s.model_dump() for s in strokes.items],
                       'limits': {'maxStrokes': cfg.max_strokes}})
        await mutate(send_init)

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
                if kind == 'draw':
                    draw = DrawMsg.model_validate(msg)
                    room.broadcast({'t': 'draw', 'from': peer.id, **draw.model_dump()}, exclude=peer)

                elif kind == 'cursor':
                    x, y = msg.get('x'), msg.get('y')
                    if x is None or (_finite(x) and _finite(y)):
                        room.broadcast({'t': 'cursor', 'id': peer.id, 'x': x, 'y': y}, exclude=peer)

                elif kind == 'stroke':
                    stroke = StrokeDTO.model_validate(msg.get('stroke'))

                    async def add():
                        try:
                            saved = await run('strokes.add', AddStrokeDTO(
                                board_id=board_id, author=peer.author, stroke=stroke, max_strokes=cfg.max_strokes))
                        except ConflictError as exc:
                            peer.send({'t': 'reject', 'id': stroke.id, 'message': exc.message})
                            return
                        room.broadcast({'t': 'stroke', 'from': peer.id, 'stroke': saved.model_dump()})
                    await mutate(add)

                elif kind == 'remove':
                    dto = RemoveStrokeDTO(board_id=board_id, author=peer.author, key=msg.get('id'))

                    async def remove():
                        try:
                            await run('strokes.remove', dto)
                        except NotFoundError:
                            return  # уже удалён или чужой: состояние у всех и так совпадает
                        room.broadcast({'t': 'remove', 'id': dto.key, 'from': peer.id})
                    await mutate(remove)

                elif kind == 'clear':
                    async def clear():
                        await run('strokes.clear', BoardIdDTO(board_id=board_id))
                        room.broadcast({'t': 'clear', 'by': peer.name, 'from': peer.id})
                    await mutate(clear)

                elif kind == 'rename':
                    name = HelloMsg(t='hello', author=peer.author, name=str(msg.get('name', ''))).name
                    peer.name = name
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
                # Сбой одной операции (например, БД) не обрывает сеанс; клиент пересинхронизируется.
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
