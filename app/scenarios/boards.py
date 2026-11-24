"""Артефакт «доска»: доски, штрихи, чат, выгрузка/загрузка и служебная проверка готовности БД."""
import hashlib
import json
import math
import re
import secrets
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from core import BaseScenario, ConflictError, NotFoundError, register_scenario

BOARD_ID_PATTERN = r'^[A-Za-z0-9_-]{16,40}$'
KEY_PATTERN = r'^[A-Za-z0-9_-]{8,40}$'
COORD_LIMIT = 1_000_000
Tool = Literal['pen', 'eraser', 'line', 'rect', 'ellipse']


class CreateBoardDTO(BaseModel):
    title: str = Field(default='', max_length=120)

    @field_validator('title')
    @classmethod
    def strip_title(cls, value):
        return ' '.join(value.split())


class BoardIdDTO(BaseModel):
    board_id: str = Field(pattern=BOARD_ID_PATTERN)


class BoardResponse(BaseModel):
    id: str
    title: str
    created_at: datetime


class StrokeData(BaseModel):
    """Содержимое штриха без ключа. pts — плоский список [x0, y0, x1, y1, ...]."""
    tool: Tool
    color: str = Field(pattern=r'^#[0-9a-fA-F]{6}$')
    width: int = Field(ge=1, le=96)
    pts: list[float] = Field(min_length=2, max_length=8000)

    @field_validator('pts')
    @classmethod
    def check_points(cls, value):
        if len(value) % 2:
            raise ValueError('pts must contain x,y pairs')
        if not all(math.isfinite(v) and abs(v) <= COORD_LIMIT for v in value):
            raise ValueError('coordinates out of range')
        return [round(v, 1) for v in value]


class StrokeDTO(StrokeData):
    """Законченный штрих с ключом, который назначает клиент."""
    id: str = Field(pattern=KEY_PATTERN)


class StrokeOut(StrokeDTO):
    author: str


class AddStrokeDTO(BaseModel):
    board_id: str = Field(pattern=BOARD_ID_PATTERN)
    author: str = Field(pattern=KEY_PATTERN)
    stroke: StrokeDTO
    max_strokes: int = Field(gt=0)


class RemoveStrokeDTO(BaseModel):
    board_id: str = Field(pattern=BOARD_ID_PATTERN)
    author: str = Field(pattern=KEY_PATTERN)
    key: str = Field(pattern=KEY_PATTERN)


class StrokeListResponse(BaseModel):
    items: list[StrokeOut]


class ClearResponse(BaseModel):
    removed: int


class ReadyResponse(BaseModel):
    status: Literal['ready']


def _stroke_out(row):
    return StrokeOut(id=row['stroke_key'], author=row['author_key'], tool=row['tool'],
                     color=row['color'], width=row['width'], pts=json.loads(row['points']))


@register_scenario('boards.create', dto=CreateBoardDTO, response=BoardResponse)
class CreateBoard(BaseScenario):
    async def execute(self, dto):
        # Явный SQL: CRUD event-infra передал бы NULL в created_at вместо серверного default.
        rows = await self._db.query(
            'INSERT INTO boards (id, title) VALUES (:id, :title) RETURNING id, title, created_at',
            {'id': secrets.token_urlsafe(16), 'title': dto.title}, channel='write')
        return BoardResponse(**rows[0])


@register_scenario('boards.get', dto=BoardIdDTO, response=BoardResponse)
class GetBoard(BaseScenario):
    async def execute(self, dto):
        row = await self._db.read('boards', dto.board_id)
        if row is None:
            raise NotFoundError('Доска не найдена')
        return BoardResponse(**row)


@register_scenario('strokes.list', dto=BoardIdDTO, response=StrokeListResponse)
class ListStrokes(BaseScenario):
    async def execute(self, dto):
        rows = await self._db.query(
            'SELECT stroke_key, author_key, tool, color, width, points FROM strokes '
            'WHERE board_id = :board_id ORDER BY id', {'board_id': dto.board_id})
        return StrokeListResponse(items=[_stroke_out(r) for r in rows])


@register_scenario('strokes.add', dto=AddStrokeDTO, response=StrokeOut)
class AddStroke(BaseScenario):
    async def execute(self, dto):
        s = dto.stroke
        params = {'board_id': dto.board_id, 'key': s.id, 'author': dto.author, 'tool': s.tool,
                  'color': s.color.lower(), 'width': s.width, 'points': json.dumps(s.pts, separators=(',', ':')),
                  'max': dto.max_strokes}
        # Лимит и вставка одним выражением; повтор того же ключа после переподключения игнорируется.
        rows = await self._db.query(
            'INSERT INTO strokes (board_id, stroke_key, author_key, tool, color, width, points) '
            'SELECT CAST(:board_id AS varchar), CAST(:key AS varchar), CAST(:author AS varchar), '
            'CAST(:tool AS varchar), CAST(:color AS varchar), CAST(:width AS smallint), CAST(:points AS text) '
            'WHERE (SELECT count(*) FROM strokes WHERE board_id = CAST(:board_id AS varchar)) < CAST(:max AS bigint) '
            'ON CONFLICT (board_id, stroke_key) DO NOTHING '
            'RETURNING stroke_key, author_key, tool, color, width, points', params, channel='write')
        if rows:
            return _stroke_out(rows[0])
        existing = await self._db.query(
            'SELECT stroke_key, author_key, tool, color, width, points FROM strokes '
            'WHERE board_id = :board_id AND stroke_key = :key', {'board_id': dto.board_id, 'key': s.id})
        if existing:
            raise ConflictError('Штрих уже сохранён')
        raise ConflictError('Доска заполнена: очистите её или создайте новую')


@register_scenario('strokes.remove', dto=RemoveStrokeDTO)
class RemoveStroke(BaseScenario):
    async def execute(self, dto):
        rows = await self._db.query(
            'DELETE FROM strokes WHERE board_id = :board_id AND stroke_key = :key AND author_key = :author '
            'RETURNING stroke_key', dto.model_dump(), channel='write')
        if not rows:
            raise NotFoundError('Штрих не найден или принадлежит другому участнику')


@register_scenario('strokes.clear', dto=BoardIdDTO, response=ClearResponse)
class ClearStrokes(BaseScenario):
    async def execute(self, dto):
        rows = await self._db.query(
            'WITH d AS (DELETE FROM strokes WHERE board_id = :board_id RETURNING 1) SELECT count(*) AS n FROM d',
            {'board_id': dto.board_id}, channel='write')
        return ClearResponse(removed=rows[0]['n'] if rows else 0)


# ---------------------------------------------------------------- выгрузка и загрузка

class BoardExport(BaseModel):
    """Файл доски: читается человеком и загружается обратно в любую доску."""
    format: Literal['doska'] = 'doska'
    version: Literal[1] = 1
    title: str = Field(default='', max_length=120)
    exported_at: datetime | None = None
    strokes: list[StrokeData] = Field(default_factory=list)

    @field_validator('title')
    @classmethod
    def strip_title(cls, value):
        return ' '.join(value.split())


class ImportStrokesDTO(BaseModel):
    board_id: str = Field(pattern=BOARD_ID_PATTERN)
    author: str = Field(pattern=KEY_PATTERN)
    replace: bool = False
    strokes: list[StrokeData]
    max_strokes: int = Field(gt=0)


class ImportResult(BaseModel):
    replace: bool
    items: list[StrokeOut]


@register_scenario('boards.export', dto=BoardIdDTO, response=BoardExport)
class ExportBoard(BaseScenario):
    async def execute(self, dto):
        board = await self._db.read('boards', dto.board_id)
        if board is None:
            raise NotFoundError('Доска не найдена')
        rows = await self._db.query(
            'SELECT tool, color, width, points FROM strokes WHERE board_id = :board_id ORDER BY id',
            {'board_id': dto.board_id})
        return BoardExport(title=board['title'], exported_at=datetime.now().astimezone(),
                           strokes=[StrokeData(tool=r['tool'], color=r['color'], width=r['width'],
                                               pts=json.loads(r['points'])) for r in rows])


@register_scenario('strokes.import', dto=ImportStrokesDTO, response=ImportResult)
class ImportStrokes(BaseScenario):
    async def execute(self, dto):
        existing = 0
        if not dto.replace:
            rows = await self._db.query('SELECT count(*) AS n FROM strokes WHERE board_id = :board_id',
                                        {'board_id': dto.board_id})
            existing = rows[0]['n']
        if existing + len(dto.strokes) > dto.max_strokes:
            raise ConflictError(f'На доске помещается не больше {dto.max_strokes} штрихов: '
                                f'сейчас {existing}, в файле {len(dto.strokes)}')
        # Ключи назначает сервер: один файл можно загружать повторно и в разные доски.
        params = {'board_id': dto.board_id, 'author': dto.author, 'replace': dto.replace,
                  'keys': [secrets.token_urlsafe(15) for _ in dto.strokes],
                  'tools': [s.tool for s in dto.strokes], 'colors': [s.color.lower() for s in dto.strokes],
                  'widths': [s.width for s in dto.strokes],
                  'points': [json.dumps(s.pts, separators=(',', ':')) for s in dto.strokes]}
        # Очистка и вставка одним выражением: при ошибке доска остаётся как была.
        rows = await self._db.query(
            'WITH d AS (DELETE FROM strokes WHERE board_id = CAST(:board_id AS varchar) '
            'AND CAST(:replace AS boolean) RETURNING 1) '
            'INSERT INTO strokes (board_id, stroke_key, author_key, tool, color, width, points) '
            'SELECT CAST(:board_id AS varchar), u.k, CAST(:author AS varchar), u.t, u.c, u.w, u.p '
            'FROM unnest(CAST(:keys AS varchar[]), CAST(:tools AS varchar[]), CAST(:colors AS varchar[]), '
            'CAST(:widths AS smallint[]), CAST(:points AS text[])) WITH ORDINALITY AS u(k, t, c, w, p, n) '
            'ORDER BY u.n '
            'RETURNING id, stroke_key, author_key, tool, color, width, points', params, channel='write')
        rows.sort(key=lambda r: r['id'])
        return ImportResult(replace=dto.replace, items=[_stroke_out(r) for r in rows])


# ---------------------------------------------------------------- чат

CHAT_MAX_CHARS = 1000


def author_tag(author: str) -> str:
    """Открытая метка автора: свои сообщения узнаются, а секретный ключ автора не раскрывается."""
    return hashlib.sha256(author.encode()).hexdigest()[:12]


def clean_chat_text(value: str) -> str:
    lines = value.replace('\r\n', '\n').replace('\r', '\n').replace('\t', ' ').split('\n')
    text = '\n'.join(''.join(ch for ch in line if ch.isprintable()).rstrip() for line in lines).strip()
    return re.sub(r'\n{3,}', '\n\n', text)


class AddChatDTO(BaseModel):
    board_id: str = Field(pattern=BOARD_ID_PATTERN)
    author: str = Field(pattern=KEY_PATTERN)
    name: str = Field(min_length=1, max_length=32)
    color: str = Field(pattern=r'^#[0-9a-fA-F]{6}$')
    text: str = Field(max_length=CHAT_MAX_CHARS * 2)
    keep: int = Field(gt=0)

    @field_validator('text')
    @classmethod
    def clean_text(cls, value):
        value = clean_chat_text(value)
        if not value:
            raise ValueError('empty message')
        if len(value) > CHAT_MAX_CHARS:
            raise ValueError('message too long')
        return value


class ChatListDTO(BaseModel):
    board_id: str = Field(pattern=BOARD_ID_PATTERN)
    limit: int = Field(gt=0, le=1000)


class ChatMessageOut(BaseModel):
    id: int
    tag: str
    name: str
    color: str
    text: str
    at: datetime


class ChatListResponse(BaseModel):
    items: list[ChatMessageOut]


def _chat_out(row):
    return ChatMessageOut(id=row['id'], tag=author_tag(row['author_key']), name=row['name'], color=row['color'],
                          text=row['text'], at=row['created_at'])


@register_scenario('chat.add', dto=AddChatDTO, response=ChatMessageOut)
class AddChatMessage(BaseScenario):
    async def execute(self, dto):
        params = {'board_id': dto.board_id, 'author': dto.author, 'name': dto.name, 'color': dto.color.lower(),
                  'text': dto.text, 'skip': dto.keep - 1}
        # Новое сообщение и обрезка старой истории (хранятся последние keep) одним выражением.
        rows = await self._db.query(
            'WITH ins AS (INSERT INTO chat_messages (board_id, author_key, name, color, text) '
            'VALUES (CAST(:board_id AS varchar), CAST(:author AS varchar), CAST(:name AS varchar), '
            'CAST(:color AS varchar), CAST(:text AS text)) '
            'RETURNING id, author_key, name, color, text, created_at), '
            'old AS (DELETE FROM chat_messages WHERE board_id = CAST(:board_id AS varchar) AND id <= ('
            'SELECT id FROM chat_messages WHERE board_id = CAST(:board_id AS varchar) '
            'ORDER BY id DESC OFFSET CAST(:skip AS bigint) LIMIT 1)) '
            'SELECT id, author_key, name, color, text, created_at FROM ins', params, channel='write')
        return _chat_out(rows[0])


@register_scenario('chat.list', dto=ChatListDTO, response=ChatListResponse)
class ListChatMessages(BaseScenario):
    async def execute(self, dto):
        rows = await self._db.query(
            'SELECT id, author_key, name, color, text, created_at FROM ('
            'SELECT id, author_key, name, color, text, created_at FROM chat_messages '
            'WHERE board_id = :board_id ORDER BY id DESC LIMIT :limit) t ORDER BY id', dto.model_dump())
        return ChatListResponse(items=[_chat_out(r) for r in rows])


class EmptyDTO(BaseModel):
    pass


@register_scenario('service.ready', dto=EmptyDTO, response=ReadyResponse)
class Ready(BaseScenario):
    async def execute(self, dto):
        await self._db.query('SELECT 1 AS ok', {})
        return ReadyResponse(status='ready')
