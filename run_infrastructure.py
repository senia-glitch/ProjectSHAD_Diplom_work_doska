"""Explicit-migration profile: startup checks revision; never autogenerates."""
from app.config import database_url
from infrastructure.event_infrastructure import create_pipeline
from infrastructure.event_infrastructure.config.models import ChannelConfig, CacheConfig, RetryConfig
from models import get_all_schemas
from scripts.migrate import assert_current


async def start_infrastructure():
    assert_current()
    return await create_pipeline(
        db_url=database_url(asynchronous=True).render_as_string(hide_password=False),
        schemas=get_all_schemas(),
        channels={name: ChannelConfig(pool_size=2, max_overflow=0) for name in ('read', 'write', 'admin')},
        cache=CacheConfig(enabled=False), retry=RetryConfig(max_retries=0),
        default_timeout=15, shutdown_timeout=10, max_concurrency=20)
