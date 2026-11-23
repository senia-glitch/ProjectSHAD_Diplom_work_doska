import pytest
from app.config import validate_test_target, database_url


def test_guard_compares_database_names(monkeypatch):
    monkeypatch.setenv('POSTGRES_DB', 'same')
    monkeypatch.setenv('POSTGRES_TEST_DB', 'same')
    with pytest.raises(ValueError):
        validate_test_target('same')


def test_guard_requires_exact_confirmation(monkeypatch):
    monkeypatch.setenv('POSTGRES_DB', 'work')
    monkeypatch.setenv('POSTGRES_TEST_DB', 'work_test')
    with pytest.raises(ValueError):
        validate_test_target('other')
    assert validate_test_target('work_test') == 'work_test'


def test_password_is_encoded_by_url_builder(monkeypatch):
    monkeypatch.setenv('POSTGRES_DB', 'work')
    monkeypatch.setenv('POSTGRES_PASSWORD', 'special@:#/password')
    assert database_url(test=False).password == 'special@:#/password'
