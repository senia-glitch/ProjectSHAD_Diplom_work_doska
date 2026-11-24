"""Сообщения чата доски."""
from alembic import op
import sqlalchemy as sa

revision = '0003_chat'
down_revision = '0002_boards'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'chat_messages',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('board_id', sa.String(40), sa.ForeignKey('boards.id', ondelete='CASCADE'), nullable=False),
        sa.Column('author_key', sa.String(40), nullable=False),
        sa.Column('name', sa.String(32), nullable=False),
        sa.Column('color', sa.String(7), nullable=False),
        sa.Column('text', sa.Text, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('ix_chat_messages_board_id_id', 'chat_messages', ['board_id', 'id'])


def downgrade():
    op.drop_index('ix_chat_messages_board_id_id', table_name='chat_messages')
    op.drop_table('chat_messages')
