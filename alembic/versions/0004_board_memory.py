"""Доска в памяти сервера с автосохранением: код входа, создатель, активность.

Штрихи становятся снимком, который перезаписывается целиком, поэтому
уникальность stroke_key в БД больше не нужна (её обеспечивает память сервера).
"""
from alembic import op
import sqlalchemy as sa

revision = '0004_board_memory'
down_revision = '0003_chat'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('boards', sa.Column('code_hash', sa.String(200), nullable=True))
    op.add_column('boards', sa.Column('creator_key', sa.String(40), nullable=True))
    op.add_column('boards', sa.Column('last_active_at', sa.DateTime(timezone=True), nullable=False,
                                      server_default=sa.text('now()')))
    op.add_column('boards', sa.Column('saved_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_boards_last_active_at', 'boards', ['last_active_at'])
    op.drop_constraint('uq_strokes_board_key', 'strokes', type_='unique')


def downgrade():
    raise RuntimeError('Downgrade requires a reviewed data migration (duplicate stroke keys are possible)')
