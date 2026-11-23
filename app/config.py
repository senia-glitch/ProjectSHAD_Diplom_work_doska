"""Project-owned configuration. Import this before core or fastbase."""
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env', override=False)
os.environ['CORE_SKIP_DOTENV'] = '1'


def database_name(test=False):
    return os.getenv('POSTGRES_TEST_DB' if test else 'POSTGRES_DB', '')


def validate_test_target(confirmed):
    production, test = database_name(), database_name(True)
    if not production or not test or production == test or confirmed != test:
        raise ValueError('A distinct, explicitly confirmed test database is required')
    return test


def database_url(*, test=None, asynchronous=False):
    if test is None:
        test = os.getenv('FACTORY_TEST_MODE') == '1'
    name = database_name(test)
    password = os.getenv('POSTGRES_PASSWORD', '')
    if not name or not password or password == '__GENERATE__':
        raise ValueError('Run scripts.configure and provide database configuration')
    if test:
        validate_test_target(os.getenv('FACTORY_TEST_DB_CONFIRM', ''))
    return URL.create('postgresql+asyncpg' if asynchronous else 'postgresql+psycopg',
                      username=os.getenv('POSTGRES_USER', 'factory'), password=password,
                      host=os.getenv('DB_HOST', '127.0.0.1'), port=int(os.getenv('DB_PORT', '5433')),
                      database=name)
