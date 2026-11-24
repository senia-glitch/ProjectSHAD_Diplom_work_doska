"""Живые доски в памяти процесса: участники, штрихи, чат, курсоры и автосохранение.

Доска существует в памяти, пока на ней есть участники. При входе первого участника
она поднимается из снимка в PostgreSQL. Автосохранение записывает изменения каждые
N минут, а также при уходе последнего участника и при остановке сервера.

Одна доска всегда обслуживается одним процессом. Несколько процессов делят доски
между собой: nginx направляет все запросы доски в один процесс по hash(board_id).
"""
import asyncio
import json
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core import run
from fastapi import WebSocket

from app.scenarios.boards import (BoardIdDTO, SaveBoardDTO, StoredChat, StoredStroke, StrokeData, TouchDTO,
                                  author_tag)

log = logging.getLogger('app.hub')

PEER_COLORS = ('#7177f8', '#e5484d', '#30a46c', '#f76b15', '#8e4ec6', '#12a594', '#d6409f', '#0090ff',
               '#ad7f58', '#e5a000', '#5b8def', '#c2410c', '#0f766e', '#be185d', '#4d7c0f')
CURSOR_FLUSH_SECONDS = 0.1  # курсоры рассылаются пачкой не чаще 10 раз в секунду


def _dumps(message) -> str:
    return json.dumps(message, separators=(',', ':'), ensure_ascii=False)


class SlowConsumer(Exception):
    pass


@dataclass(eq=False)
class Peer:
    ws: WebSocket
    author: str
    tag: str
    name: str
    color: str
    queue: asyncio.Queue
    creator: bool = False
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    closed: bool = False

    def public(self):
        return {'id': self.id, 'name': self.name, 'color': self.color, 'creator': self.creator}

    def send(self, message: dict | str):
        if self.closed:
            return
        try:
            self.queue.put_nowait(message if isinstance(message, str) else _dumps(message))
        except asyncio.QueueFull:
            # Клиент не успевает читать: закрываем его, а не копим память сервера.
            self.closed = True
            raise SlowConsumer()


@dataclass(slots=True)
class StrokeRec:
    key: str
    author: str
    tag: str
    tool: str
    color: str
    width: int
    pts_json: str
    values: int

    def wire(self) -> str:
        # Поля проверены схемой (ключ, цвет, инструмент), pts — уже готовый JSON.
        return (f'{{"id":"{self.key}","tool":"{self.tool}","color":"{self.color}","width":{self.width},'
                f'"by":"{self.tag}","pts":{self.pts_json}}}')


@dataclass(slots=True)
class ChatRec:
    id: int
    author: str
    tag: str
    name: str
    color: str
    text: str
    at: datetime

    def public(self):
        return {'id': self.id, 'tag': self.tag, 'name': self.name, 'color': self.color, 'text': self.text,
                'at': self.at.isoformat()}


class LimitError(Exception):
    pass


class Room:
    def __init__(self, board_id: str, title: str, creator_key: str | None, *, chat_keep: int,
                 saved_at: datetime | None):
        self.board_id = board_id
        self.title = title
        self.creator_key = creator_key
        self.peers: dict[str, Peer] = {}
        self.save_lock = asyncio.Lock()
        self.holders = 0
        self.strokes: dict[str, StrokeRec] = {}
        self.values = 0
        self.chat: deque[ChatRec] = deque(maxlen=chat_keep)
        self.chat_seq = 0
        self.version = 0
        self.saved_version = 0
        self.saved_at = saved_at
        self.last_save = time.monotonic()
        self.cursors: dict[str, list | None] = {}
        self.cursor_task: asyncio.Task | None = None

    # ------------------------------------------------ участники и рассылка

    def next_color(self):
        used = {p.color for p in self.peers.values()}
        for color in PEER_COLORS:
            if color not in used:
                return color
        return PEER_COLORS[len(self.peers) % len(PEER_COLORS)]

    def broadcast(self, message: dict | str, exclude: Peer | None = None):
        payload = message if isinstance(message, str) else _dumps(message)
        for peer in list(self.peers.values()):
            if peer is exclude:
                continue
            try:
                peer.send(payload)
            except SlowConsumer:
                asyncio.get_running_loop().create_task(_close(peer, 1013, 'slow consumer'))

    async def cursor_loop(self):
        while True:
            await asyncio.sleep(CURSOR_FLUSH_SECONDS)
            if self.cursors:
                batch = [[pid, *(xy or (None, None))] for pid, xy in self.cursors.items()]
                self.cursors.clear()
                self.broadcast({'t': 'cursors', 'c': batch})

    # ------------------------------------------------ содержимое доски

    def touch(self):
        self.version += 1

    @property
    def dirty(self) -> bool:
        return self.version != self.saved_version

    def usage(self):
        return {'strokes': len(self.strokes), 'values': self.values, 'chat': len(self.chat)}

    def make_stroke(self, data: StrokeData, key: str, author: str) -> StrokeRec:
        return StrokeRec(key=key, author=author, tag=author_tag(author), tool=data.tool, color=data.color,
                         width=data.width, pts_json=json.dumps(data.pts, separators=(',', ':')),
                         values=len(data.pts))

    def check_capacity(self, strokes: int, values: int, max_strokes: int, max_values: int):
        if len(self.strokes) + strokes > max_strokes or self.values + values > max_values:
            raise LimitError('Доска заполнена: очистите её, отмените часть штрихов или создайте новую')

    def add_stroke(self, rec: StrokeRec):
        self.strokes[rec.key] = rec
        self.values += rec.values
        self.touch()

    def remove_stroke(self, key: str, author: str) -> bool:
        rec = self.strokes.get(key)
        if rec is None or rec.author != author:
            return False
        del self.strokes[key]
        self.values -= rec.values
        self.touch()
        return True

    def clear(self):
        self.strokes.clear()
        self.values = 0
        self.touch()

    def add_chat(self, peer: Peer, text: str) -> ChatRec:
        self.chat_seq += 1
        rec = ChatRec(id=self.chat_seq, author=peer.author, tag=peer.tag, name=peer.name, color=peer.color,
                      text=text, at=datetime.now(timezone.utc))
        self.chat.append(rec)
        self.touch()
        return rec

    def init_message(self, peer: Peer, limits: dict) -> str:
        head = _dumps({'t': 'init', 'you': {**peer.public(), 'tag': peer.tag},
                       'board': {'id': self.board_id, 'title': self.title},
                       'peers': [p.public() for p in self.peers.values() if p is not peer],
                       'chat': [m.public() for m in self.chat],
                       'savedAt': self.saved_at.isoformat() if self.saved_at else None,
                       'limits': limits})
        return head[:-1] + ',"strokes":[' + ','.join(r.wire() for r in self.strokes.values()) + ']}'

    def snapshot(self) -> SaveBoardDTO:
        return SaveBoardDTO.model_construct(
            board_id=self.board_id,
            strokes=[StoredStroke.model_construct(key=r.key, author=r.author, tool=r.tool, color=r.color,
                                                  width=r.width, pts_json=r.pts_json) for r in self.strokes.values()],
            chat=[StoredChat.model_construct(author=m.author, name=m.name, color=m.color, text=m.text, at=m.at)
                  for m in self.chat])


async def _close(peer: Peer, code: int, reason: str):
    peer.closed = True
    try:
        await peer.ws.close(code=code, reason=reason)
    except Exception:
        pass


class Hub:
    def __init__(self, chat_keep: int = 300):
        self.chat_keep = chat_keep
        self.rooms: dict[str, Room] = {}
        self._loading: dict[str, asyncio.Future] = {}
        self._unloading: set[asyncio.Task] = set()

    # ------------------------------------------------ жизненный цикл комнаты

    async def open(self, board_id: str) -> Room:
        """Комната доски с удержанием: пока holders > 0, комната не выгружается. Парно с release()."""
        while True:
            room = self.rooms.get(board_id)
            if room is not None:
                room.holders += 1
                return room
            pending = self._loading.get(board_id)
            if pending is not None:
                try:
                    await asyncio.shield(pending)
                except Exception:
                    pass  # загрузка у другого вызова не удалась: пробуем сами
                continue
            future = asyncio.get_running_loop().create_future()
            self._loading[board_id] = future
            try:
                snap = await run('boards.load', BoardIdDTO(board_id=board_id))
                room = Room(board_id, snap.title, snap.creator_key, chat_keep=self.chat_keep, saved_at=snap.saved_at)
                for s in snap.strokes:
                    room.strokes[s.key] = StrokeRec(
                        key=s.key, author=s.author, tag=author_tag(s.author), tool=s.tool, color=s.color,
                        width=s.width, pts_json=s.pts_json, values=s.pts_json.count(',') + 1)
                    room.values += room.strokes[s.key].values
                for m in snap.chat[-self.chat_keep:]:
                    room.chat_seq += 1
                    room.chat.append(ChatRec(id=room.chat_seq, author=m.author, tag=author_tag(m.author), name=m.name,
                                             color=m.color, text=m.text, at=m.at))
                room.cursor_task = asyncio.get_running_loop().create_task(room.cursor_loop())
                self.rooms[board_id] = room
                room.holders += 1
                future.set_result(None)
                return room
            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)
                    future.exception()  # помечено как полученное; ожидающие повторят загрузку
                raise
            finally:
                self._loading.pop(board_id, None)

    def release(self, room: Room):
        room.holders -= 1
        self._maybe_unload(room)

    def join(self, room: Room, peer: Peer):
        room.peers[peer.id] = peer
        room.broadcast({'t': 'join', 'peer': peer.public()}, exclude=peer)

    def leave(self, room: Room, peer: Peer):
        if room.peers.pop(peer.id, None) is not None:
            room.cursors.pop(peer.id, None)
            room.broadcast({'t': 'leave', 'id': peer.id})
        self._maybe_unload(room)

    async def kick(self, room: Room, peer: Peer):
        """Исключение: соединение закрывается с 4410, остальные участники остаются на доске."""
        room.broadcast({'t': 'kicked', 'id': peer.id, 'name': peer.name}, exclude=peer)
        await _close(peer, 4410, 'kicked')

    def _maybe_unload(self, room: Room):
        if not room.peers and room.holders <= 0 and self.rooms.get(room.board_id) is room:
            task = asyncio.get_running_loop().create_task(self._unload(room))
            self._unloading.add(task)
            task.add_done_callback(self._unloading.discard)

    async def _unload(self, room: Room):
        """Последний участник ушёл: сохранить и убрать доску из памяти."""
        try:
            await self.save(room)
        except Exception:
            log.exception('save on unload failed: %s', room.board_id)
            return  # доска остаётся в памяти; автосохранение повторит попытку
        if not room.peers and room.holders <= 0 and self.rooms.get(room.board_id) is room:
            del self.rooms[room.board_id]
            if room.cursor_task:
                room.cursor_task.cancel()

    # ------------------------------------------------ сохранение

    async def save(self, room: Room, force: bool = False) -> bool:
        async with room.save_lock:
            if not room.dirty and not force:
                return False
            version = room.version
            snapshot = room.snapshot()  # синхронно: снимок согласован, изменения продолжаются
            result = await run('boards.save', snapshot)
            room.saved_version = version
            room.saved_at = result.saved_at
            room.last_save = time.monotonic()
        room.broadcast({'t': 'saved', 'at': result.saved_at.isoformat()})
        return True

    async def autosave_loop(self, interval_seconds: float, check_seconds: float = 15):
        last_touch = time.monotonic()
        while True:
            await asyncio.sleep(min(check_seconds, interval_seconds))
            now = time.monotonic()
            for room in list(self.rooms.values()):
                if room.dirty and now - room.last_save >= interval_seconds:
                    try:
                        await self.save(room)
                    except Exception:
                        log.exception('autosave failed: %s', room.board_id)
                elif not room.dirty:
                    room.last_save = now
            if now - last_touch >= interval_seconds and self.rooms:
                last_touch = now
                try:  # открытые без изменений доски тоже активны
                    await run('boards.touch', TouchDTO(board_ids=list(self.rooms)))
                except Exception:
                    log.exception('touch failed')

    async def shutdown(self):
        """Остановка процесса: сохранить все доски, клиенты переподключатся (1012)."""
        if self._unloading:
            await asyncio.gather(*self._unloading, return_exceptions=True)
        for room in list(self.rooms.values()):
            try:
                await self.save(room)
            except Exception:
                log.exception('save on shutdown failed: %s', room.board_id)
            if room.cursor_task:
                room.cursor_task.cancel()
            for peer in list(room.peers.values()):
                await _close(peer, 1012, 'server restart')
        self.rooms.clear()

    def stats(self):
        return {'rooms': len(self.rooms), 'peers': sum(len(r.peers) for r in self.rooms.values())}


class RateLimiter:
    """Маркерное ведро: rate событий в секунду с запасом burst."""

    def __init__(self, rate: float, burst: float):
        self.rate, self.capacity = rate, burst
        self.tokens, self.stamp = burst, time.monotonic()

    def allow(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


class KeyedRateLimiter:
    """Ограничение частоты по ключу (адрес клиента) с вытеснением старых ключей."""

    def __init__(self, per_minute: int, max_keys: int = 10000):
        self.per_minute, self.max_keys = per_minute, max_keys
        self.buckets: dict[str, RateLimiter] = {}

    def allow(self, key: str) -> bool:
        bucket = self.buckets.get(key)
        if bucket is None:
            if len(self.buckets) >= self.max_keys:
                self.buckets.pop(next(iter(self.buckets)))
            bucket = self.buckets[key] = RateLimiter(self.per_minute / 60, self.per_minute)
        return bucket.allow()


def _make_hub() -> Hub:
    from app.config import board_settings
    return Hub(chat_keep=board_settings.chat_keep)


hub = _make_hub()
