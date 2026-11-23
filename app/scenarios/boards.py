"""Артефакт «доска»: доски, штрихи и служебная проверка готовности БД."""
import json
import math
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


class StrokeDTO(BaseModel):
    """Законченный штрих. pts — плоский список [x0, y0, x1, y1, ...]."""
    id: str = Field(pattern=KEY_PATTERN)
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


class EmptyDTO(BaseModel):
    pass


@register_scenario('service.ready', dto=EmptyDTO, response=ReadyResponse)
class Ready(BaseScenario):
    async def execute(self, dto):
        await self._db.query('SELECT 1 AS ok', {})
        return ReadyResponse(status='ready')
