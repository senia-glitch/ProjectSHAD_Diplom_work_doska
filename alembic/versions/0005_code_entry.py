"""Вход только по коду: уникальный ключ поиска кода, участники доски и исключения.

code_hash (scrypt с солью) не позволяет найти доску по коду, поэтому заменяется
на code_lookup (HMAC с секретом сервера) с уникальным индексом. Восстановить код
из scrypt нельзя: доски, созданные до этой версии, остаются без кода и недоступны
для входа; они удалятся по сроку хранения (BOARD_RETENTION_DAYS).
"""
from alembic import op
import sqlalchemy as sa

revision = '0005_code_entry'
down_revision = '0004_board_memory'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('boards', sa.Column('code_lookup', sa.String(64), nullable=True))
    op.create_unique_constraint('boards_code_lookup_key', 'boards', ['code_lookup'])
    op.drop_column('boards', 'code_hash')
    op.create_table(
        'board_members',
        sa.Column('board_id', sa.String(40), sa.ForeignKey('boards.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('author_key', sa.String(40), primary_key=True),
        sa.Column('banned', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('joined_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )


def downgrade():
    raise RuntimeError('Downgrade would need the original access codes, which are not stored')
