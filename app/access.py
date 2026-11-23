"""Проверка единственной роли участника по config/access.toml до вызова core."""
import tomllib

from fastbase import ForbiddenError

from app.config import ROOT

POLICY_PATH = ROOT / 'config' / 'access.toml'


def load_policy(path=POLICY_PATH):
    with open(path, 'rb') as fh:
        data = tomllib.load(fh)
    roles = data.get('roles')
    if not isinstance(roles, dict) or not roles:
        raise RuntimeError('Access policy has no roles')
    policy = {}
    for role, spec in roles.items():
        scenarios = spec.get('scenarios') if isinstance(spec, dict) else None
        if not isinstance(scenarios, list) or not all(isinstance(s, str) and s for s in scenarios):
            raise RuntimeError(f'Access policy role {role!r} is malformed')
        policy[role] = frozenset(scenarios)
    return policy


POLICY = load_policy()


def role_of(connection) -> str:
    """Роль назначает сервер. Учётных записей нет: любой посетитель — guest."""
    return 'guest'


def is_allowed(role: str, scenario: str) -> bool:
    return scenario in POLICY.get(role, frozenset())


def require(scenario: str):
    """Depends-провайдер HTTP: проверка до вызова core."""
    from fastapi import Request

    def dependency(request: Request):
        if not is_allowed(role_of(request), scenario):
            raise ForbiddenError('Недостаточно прав')
    return dependency
