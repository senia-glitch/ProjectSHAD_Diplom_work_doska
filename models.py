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


class Stroke(SQLModel, table=True):
    __tablename__ = 'strokes'
    __table_args__ = (
        sa.UniqueConstraint('board_id', 'stroke_key', name='uq_strokes_board_key'),
        sa.Index('ix_strokes_board_id_id', 'board_id', 'id'),
    )
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


metadata = SQLModel.metadata


def get_all_schemas():
    return {'boards': Board, 'strokes': Stroke, 'chat_messages': ChatMessage}
