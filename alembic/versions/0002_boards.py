"""Доски и штрихи; удаление демонстрационной таблицы notes из каркаса.

notes создавалась только примером starter и данных продукта не содержит.
"""
from alembic import op
import sqlalchemy as sa

revision = '0002_boards'
down_revision = '0001_notes'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'boards',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('title', sa.String(120), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        'strokes',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('board_id', sa.String(40), sa.ForeignKey('boards.id', ondelete='CASCADE'), nullable=False),
        sa.Column('stroke_key', sa.String(40), nullable=False),
        sa.Column('author_key', sa.String(40), nullable=False),
        sa.Column('tool', sa.String(16), nullable=False),
        sa.Column('color', sa.String(7), nullable=False),
        sa.Column('width', sa.SmallInteger, nullable=False),
        sa.Column('points', sa.Text, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('board_id', 'stroke_key', name='uq_strokes_board_key'),
    )
    op.create_index('ix_strokes_board_id_id', 'strokes', ['board_id', 'id'])
    op.drop_table('notes')


def downgrade():
    raise RuntimeError('Destructive downgrade requires an explicit reviewed data migration')
