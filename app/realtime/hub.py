"""Комнаты досок в памяти процесса: участники, рассылка и порядок изменений.

Состояние живёт в одном процессе (FAT_WORKERS=1). Для нескольких процессов
понадобится общая шина (например, PostgreSQL LISTEN/NOTIFY или Redis).
"""
import asyncio
import json
import secrets
from contextlib import asynccontextmanager
import time
from dataclasses import dataclass, field

from fastapi import WebSocket

PEER_COLORS = ('#7177f8', '#e5484d', '#30a46c', '#f76b15', '#8e4ec6', '#12a594', '#d6409f', '#0090ff',
               '#ad7f58', '#e5a000')


class SlowConsumer(Exception):
    pass


@dataclass(eq=False)
class Peer:
    ws: WebSocket
    author: str
    name: str
    color: str
    queue: asyncio.Queue
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    closed: bool = False

    def public(self):
        return {'id': self.id, 'name': self.name, 'color': self.color}

    def send(self, message: dict | str):
        if self.closed:
            return
        payload = message if isinstance(message, str) else json.dumps(message, separators=(',', ':'),
                                                                        ensure_ascii=False)
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Клиент не успевает читать: закрываем его, а не копим память сервера.
            self.closed = True
            raise SlowConsumer()


class Room:
    def __init__(self, board_id: str):
        self.board_id = board_id
        self.peers: dict[str, Peer] = {}
        # Сериализует изменения доски и снимок для входящих: порядок у всех одинаковый.
        self.lock = asyncio.Lock()
        self.holders = 0  # операции вне WebSocket, которые сейчас держат комнату

    def next_color(self):
        used = {p.color for p in self.peers.values()}
        for color in PEER_COLORS:
            if color not in used:
                return color
        return PEER_COLORS[len(self.peers) % len(PEER_COLORS)]

    def broadcast(self, message: dict, exclude: Peer | None = None):
        payload = json.dumps(message, separators=(',', ':'), ensure_ascii=False)
        for peer in list(self.peers.values()):
            if peer is exclude:
                continue
            try:
                peer.send(payload)
            except SlowConsumer:
                asyncio.get_running_loop().create_task(_close(peer, 1013, 'slow consumer'))


async def _close(peer: Peer, code: int, reason: str):
    peer.closed = True
    try:
        await peer.ws.close(code=code, reason=reason)
    except Exception:
        pass


class Hub:
    def __init__(self):
        self.rooms: dict[str, Room] = {}

    def room(self, board_id: str) -> Room:
        room = self.rooms.get(board_id)
        if room is None:
            room = self.rooms[board_id] = Room(board_id)
        return room

    def leave(self, room: Room, peer: Peer):
        if room.peers.pop(peer.id, None) is not None:
            room.broadcast({'t': 'leave', 'id': peer.id})
        self._drop_if_idle(room)

    def _drop_if_idle(self, room: Room):
        if not room.peers and not room.holders and self.rooms.get(room.board_id) is room \
                and not room.lock.locked():
            del self.rooms[room.board_id]

    @asynccontextmanager
    async def exclusive(self, board_id: str):
        """Изменение доски вне WebSocket (загрузка файла) в общем порядке с изменениями участников."""
        room = self.room(board_id)
        room.holders += 1
        try:
            async with room.lock:
                yield room
        finally:
            room.holders -= 1
            self._drop_if_idle(room)

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


hub = Hub()
