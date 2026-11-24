"""Схема БД доски. Изменения оформляются ревизией Alembic в alembic/versions."""
from datetime import datetime

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class Board(SQLModel, table=True):
    __tablename__ = 'boards'
    # Неугадываемый токен из ссылки: secrets.token_urlsafe(16).
    id: str = Field(sa_column=sa.Column(sa.String(40), primary_key=True))
    title: str = Field(sa_column=sa.Column(sa.String(120), nullable=False, server_default=''))
    created_at: datetime | None = Field(default=None, sa_column=sa.Column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')))
    # HMAC-SHA256 кода входа с секретом сервера; уникален: код однозначно указывает на доску.
    # NULL — доска из версии без кодов: войти на неё нельзя.
    code_lookup: str | None = Field(default=None, sa_column=sa.Column(sa.String(64), nullable=True, unique=True))
    creator_key: str | None = Field(default=None, sa_column=sa.Column(sa.String(40), nullable=True))
    # Последняя активность: по ней удаляются заброшенные доски.
    last_active_at: datetime | None = Field(default=None, sa_column=sa.Column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()'), index=True))
    saved_at: datetime | None = Field(default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True))


class Stroke(SQLModel, table=True):
    __tablename__ = 'strokes'
    # Снимок доски: строки целиком перезаписываются автосохранением, порядок — по id.
    __table_args__ = (sa.Index('ix_strokes_board_id_id', 'board_id', 'id'),)
    id: int | None = Field(default=None, sa_column=sa.Column(sa.BigInteger, primary_key=True, autoincrement=True))
    board_id: str = Field(sa_column=sa.Column(
        sa.String(40), sa.ForeignKey('boards.id', ondelete='CASCADE'), nullable=False))
    stroke_key: str = Field(sa_column=sa.Column(sa.String(40), nullable=False))
    author_key: str = Field(sa_column=sa.Column(sa.String(40), nullable=False))
    tool: str = Field(sa_column=sa.Column(sa.String(16), nullable=False))
    color: str = Field(sa_column=sa.Column(sa.String(7), nullable=False))
    width: int = Field(sa_column=sa.Column(sa.SmallInteger, nullable=False))
    # Плоский JSON-массив координат [x0, y0, x1, y1, ...] в мировых единицах доски.
    points: str = Field(sa_column=sa.Column(sa.Text, nullable=False))
    created_at: datetime | None = Field(default=None, sa_column=sa.Column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')))


class ChatMessage(SQLModel, table=True):
    __tablename__ = 'chat_messages'
    __table_args__ = (sa.Index('ix_chat_messages_board_id_id', 'board_id', 'id'),)
    id: int | None = Field(default=None, sa_column=sa.Column(sa.BigInteger, primary_key=True, autoincrement=True))
    board_id: str = Field(sa_column=sa.Column(
        sa.String(40), sa.ForeignKey('boards.id', ondelete='CASCADE'), nullable=False))
    author_key: str = Field(sa_column=sa.Column(sa.String(40), nullable=False))
    # Имя и цвет на момент отправки: участник может переименоваться позже.
    name: str = Field(sa_column=sa.Column(sa.String(32), nullable=False))
    color: str = Field(sa_column=sa.Column(sa.String(7), nullable=False))
    text: str = Field(sa_column=sa.Column(sa.Text, nullable=False))
    created_at: datetime | None = Field(default=None, sa_column=sa.Column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')))


class BoardMember(SQLModel, table=True):
    """Кто входил на доску: повторный вход без кода. banned — исключён создателем."""
    __tablename__ = 'board_members'
    board_id: str = Field(sa_column=sa.Column(
        sa.String(40), sa.ForeignKey('boards.id', ondelete='CASCADE'), primary_key=True))
    author_key: str = Field(sa_column=sa.Column(sa.String(40), primary_key=True))
    banned: bool = Field(default=False, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.false()))
    joined_at: datetime | None = Field(default=None, sa_column=sa.Column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')))


metadata = SQLModel.metadata


def get_all_schemas():
    return {'boards': Board, 'strokes': Stroke, 'chat_messages': ChatMessage, 'board_members': BoardMember}
