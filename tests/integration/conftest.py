import os
import pytest
from app.config import validate_test_target

# Validate before importing the application and before any connection.
validate_test_target(os.getenv('FACTORY_TEST_DB_CONFIRM'))
os.environ['FACTORY_TEST_MODE'] = '1'


@pytest.fixture(scope='session')
def client():
    from scripts.migrate import upgrade
    upgrade(os.environ['FACTORY_TEST_DB_CONFIRM'])
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value
