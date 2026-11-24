"""Артефакт «доска»: правила данных, код входа и хранение снимков доски в PostgreSQL.

Живая доска находится в памяти процесса (app/realtime/hub.py); БД хранит её снимок,
который автосохранение перезаписывает целиком.
"""
import asyncio
import base64
import hashlib
import hmac
import math
import re
import secrets
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from core import BaseScenario, NotFoundError, register_scenario

BOARD_ID_PATTERN = r'^[A-Za-z0-9_-]{16,40}$'
KEY_PATTERN = r'^[A-Za-z0-9_-]{8,40}$'
COORD_LIMIT = 1_000_000
Tool = Literal['pen', 'eraser', 'line', 'rect', 'ellipse']

# ---------------------------------------------------------------- код входа

CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # без похожих 0/O, 1/I
GENERATED_CODE_LENGTH = 8                           # 32^8 = 2^40 вариантов
_CODE_RE = re.compile(r'^\w{4,32}$')
_SCRYPT = {'n': 2 ** 14, 'r': 8, 'p': 1}


def normalize_code(value: str) -> str:
    """Код не зависит от регистра, пробелов и дефисов: «k7mp-2qxr» = «K7MP2QXR»."""
    return re.sub(r'[\s\-]+', '', value or '').upper()


def generate_code() -> str:
    return ''.join(secrets.choice(CODE_ALPHABET) for _ in range(GENERATED_CODE_LENGTH))


def hash_code(code: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(normalize_code(code).encode(), salt=salt, dklen=32, **_SCRYPT)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_code(code: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        kind, n, r, p, salt, digest = stored.split('$')
        unb64 = lambda s: base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
        actual = hashlib.scrypt(normalize_code(code).encode(), salt=unb64(salt), dklen=32,
                                n=int(n), r=int(r), p=int(p))
    except ValueError:
        return False
    return kind == 'scrypt' and hmac.compare_digest(actual, unb64(digest))


def author_tag(author: str) -> str:
    """Открытая метка автора: свои штрихи и сообщения узнаются, секретный ключ не раскрывается."""
    return hashlib.sha256(author.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- штрихи и файл доски

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

    @field_validator('color')
    @classmethod
    def lower_color(cls, value):
        return value.lower()


class StrokeDTO(StrokeData):
    """Законченный штрих с ключом, который назначает клиент."""
    id: str = Field(pattern=KEY_PATTERN)


class BoardFile(BaseModel):
    """Файл доски .doska.json: читается человеком и загружается обратно в любую доску. Чата в нём нет."""
    format: Literal['doska'] = 'doska'
    version: Literal[1] = 1
    title: str = Field(default='', max_length=120)
    exported_at: datetime | None = None
    strokes: list[StrokeData] = Field(default_factory=list)

    @field_validator('title')
    @classmethod
    def strip_title(cls, value):
        return ' '.join(value.split())


# ---------------------------------------------------------------- чат

CHAT_MAX_CHARS = 1000


def clean_chat_text(value: str) -> str:
    lines = value.replace('\r\n', '\n').replace('\r', '\n').replace('\t', ' ').split('\n')
    text = '\n'.join(''.join(ch for ch in line if ch.isprintable()).rstrip() for line in lines).strip()
    return re.sub(r'\n{3,}', '\n\n', text)


class ChatTextDTO(BaseModel):
    text: str = Field(max_length=CHAT_MAX_CHARS * 2)

    @field_validator('text')
    @classmethod
    def clean_text(cls, value):
        value = clean_chat_text(value)
        if not value:
            raise ValueError('empty message')
        if len(value) > CHAT_MAX_CHARS:
            raise ValueError('message too long')
        return value


# ---------------------------------------------------------------- хранение

class StoredStroke(BaseModel):
    """Штрих в снимке. pts хранится готовой JSON-строкой: без разбора в числа в памяти сервера."""
    key: str
    author: str
    tool: str
    color: str
    width: int
    pts_json: str


class StoredChat(BaseModel):
    author: str
    name: str
    color: str
    text: str
    at: datetime


class CreateBoardDTO(BaseModel):
    title: str = Field(default='', max_length=120)
    author: str = Field(pattern=KEY_PATTERN)
    code: str | None = Field(default=None, max_length=64)

    @field_validator('title')
    @classmethod
    def strip_title(cls, value):
        return ' '.join(value.split())

    @field_validator('code')
    @classmethod
    def check_code(cls, value):
        if value is None or not value.strip():
            return None
        value = normalize_code(value)
        if not _CODE_RE.match(value):
            raise ValueError('Код: от 4 до 32 букв или цифр')
        return value


class BoardCreated(BaseModel):
    id: str
    title: str
    code: str
    created_at: datetime


class BoardIdDTO(BaseModel):
    board_id: str = Field(pattern=BOARD_ID_PATTERN)


class AccessDTO(BaseModel):
    board_id: str = Field(pattern=BOARD_ID_PATTERN)
    code: str = Field(max_length=64)


class BoardAccess(BaseModel):
    found: bool
    granted: bool = False
    title: str = ''
    creator_key: str | None = None


class BoardSnapshot(BaseModel):
    id: str
    title: str
    creator_key: str | None
    saved_at: datetime | None
    strokes: list[StoredStroke]
    chat: list[StoredChat]


class SaveBoardDTO(BaseModel):
    board_id: str = Field(pattern=BOARD_ID_PATTERN)
    strokes: list[StoredStroke]
    chat: list[StoredChat]


class SaveResult(BaseModel):
    saved_at: datetime


class TouchDTO(BaseModel):
    board_ids: list[str] = Field(max_length=100000)


class CleanupDTO(BaseModel):
    days: int = Field(ge=1, le=3650)


class CountResponse(BaseModel):
    count: int


class ReadyResponse(BaseModel):
    status: Literal['ready']


class EmptyDTO(BaseModel):
    pass


def _stroke_arrays(strokes: list[StoredStroke]) -> dict:
    return {'keys': [s.key for s in strokes], 'authors': [s.author for s in strokes],
            'tools': [s.tool for s in strokes], 'colors': [s.color for s in strokes],
            'widths': [s.width for s in strokes], 'points': [s.pts_json for s in strokes]}


_INSERT_STROKES = (
    'INSERT INTO strokes (board_id, stroke_key, author_key, tool, color, width, points) '
    'SELECT {board}, u.k, u.a, u.t, u.c, u.w, u.p '
    'FROM unnest(CAST(:keys AS varchar[]), CAST(:authors AS varchar[]), CAST(:tools AS varchar[]), '
    'CAST(:colors AS varchar[]), CAST(:widths AS smallint[]), CAST(:points AS text[])) '
    'WITH ORDINALITY AS u(k, a, t, c, w, p, n) ORDER BY u.n')


@register_scenario('boards.create', dto=CreateBoardDTO, response=BoardCreated)
class CreateBoard(BaseScenario):
    async def execute(self, dto):
        code = dto.code or generate_code()
        code_hash = await asyncio.to_thread(hash_code, code)
        rows = await self._db.query(
            'INSERT INTO boards (id, title, code_hash, creator_key) VALUES (:id, :title, :code_hash, :creator) '
            'RETURNING id, title, created_at',
            {'id': secrets.token_urlsafe(16), 'title': dto.title, 'code_hash': code_hash, 'creator': dto.author},
            channel='write')
        return BoardCreated(code=code, **rows[0])


@register_scenario('boards.access', dto=AccessDTO, response=BoardAccess)
class CheckAccess(BaseScenario):
    async def execute(self, dto):
        rows = await self._db.query('SELECT title, code_hash, creator_key FROM boards WHERE id = :id',
                                    {'id': dto.board_id})
        if not rows:
            return BoardAccess(found=False)
        row = rows[0]
        granted = await asyncio.to_thread(verify_code, dto.code, row['code_hash'])
        return BoardAccess(found=True, granted=granted, title=row['title'] if granted else '',
                           creator_key=row['creator_key'] if granted else None)


@register_scenario('boards.load', dto=BoardIdDTO, response=BoardSnapshot)
class LoadBoard(BaseScenario):
    async def execute(self, dto):
        boards = await self._db.query(
            'UPDATE boards SET last_active_at = now() WHERE id = :id '
            'RETURNING id, title, creator_key, saved_at', {'id': dto.board_id}, channel='write')
        if not boards:
            raise NotFoundError('Доска не найдена')
        strokes = await self._db.query(
            'SELECT stroke_key, author_key, tool, color, width, points FROM strokes '
            'WHERE board_id = :id ORDER BY id', {'id': dto.board_id})
        chat = await self._db.query(
            'SELECT author_key, name, color, text, created_at FROM chat_messages '
            'WHERE board_id = :id ORDER BY id', {'id': dto.board_id})
        return BoardSnapshot(
            **boards[0],
            strokes=[StoredStroke(key=r['stroke_key'], author=r['author_key'], tool=r['tool'], color=r['color'],
                                  width=r['width'], pts_json=r['points']) for r in strokes],
            chat=[StoredChat(author=r['author_key'], name=r['name'], color=r['color'], text=r['text'],
                             at=r['created_at']) for r in chat])


@register_scenario('boards.save', dto=SaveBoardDTO, response=SaveResult)
class SaveBoard(BaseScenario):
    async def execute(self, dto):
        params = {'board': dto.board_id, **_stroke_arrays(dto.strokes),
                  'c_authors': [m.author for m in dto.chat], 'c_names': [m.name for m in dto.chat],
                  'c_colors': [m.color for m in dto.chat], 'c_texts': [m.text for m in dto.chat],
                  'c_at': [m.at for m in dto.chat]}
        board = 'CAST(:board AS varchar)'
        # Снимок заменяется атомарно одним выражением: при сбое в БД остаётся предыдущий.
        rows = await self._db.query(
            f'WITH ds AS (DELETE FROM strokes WHERE board_id = {board}), '
            'ins AS (' + _INSERT_STROKES.format(board=board) + '), '
            f'dc AS (DELETE FROM chat_messages WHERE board_id = {board}), '
            'ic AS (INSERT INTO chat_messages (board_id, author_key, name, color, text, created_at) '
            f'SELECT {board}, u.a, u.nm, u.c, u.t, u.at '
            'FROM unnest(CAST(:c_authors AS varchar[]), CAST(:c_names AS varchar[]), CAST(:c_colors AS varchar[]), '
            'CAST(:c_texts AS text[]), CAST(:c_at AS timestamptz[])) WITH ORDINALITY AS u(a, nm, c, t, at, n) '
            'ORDER BY u.n) '
            f'UPDATE boards SET saved_at = now(), last_active_at = now() WHERE id = {board} RETURNING saved_at',
            params, channel='write')
        if not rows:
            raise NotFoundError('Доска удалена')
        return SaveResult(saved_at=rows[0]['saved_at'])


@register_scenario('boards.touch', dto=TouchDTO, response=CountResponse)
class TouchBoards(BaseScenario):
    """Открытые доски не считаются заброшенными, даже если на них ничего не меняют."""
    async def execute(self, dto):
        if not dto.board_ids:
            return CountResponse(count=0)
        rows = await self._db.query(
            'WITH u AS (UPDATE boards SET last_active_at = now() WHERE id = ANY(CAST(:ids AS varchar[])) '
            'RETURNING 1) SELECT count(*) AS n FROM u', {'ids': dto.board_ids}, channel='write')
        return CountResponse(count=rows[0]['n'])


@register_scenario('boards.cleanup', dto=CleanupDTO, response=CountResponse)
class CleanupBoards(BaseScenario):
    """Удаляет доски без активности дольше days дней; штрихи и чат удаляются каскадом."""
    async def execute(self, dto):
        rows = await self._db.query(
            'WITH d AS (DELETE FROM boards WHERE last_active_at < now() - make_interval(days => CAST(:days AS int)) '
            'RETURNING 1) SELECT count(*) AS n FROM d', {'days': dto.days}, channel='write')
        return CountResponse(count=rows[0]['n'])


@register_scenario('service.ready', dto=EmptyDTO, response=ReadyResponse)
class Ready(BaseScenario):
    async def execute(self, dto):
        await self._db.query('SELECT 1 AS ok', {})
        return ReadyResponse(status='ready')
