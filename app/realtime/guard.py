"""Защита входа на доски: частота подключений и перебор кода (с адреса, к доске, по всей системе)."""
import time
from collections import OrderedDict

from app.realtime.hub import KeyedRateLimiter


class FailureTracker:
    """Неудачи по ключу в скользящем окне; после limit неудач ключ заблокирован на lock_seconds."""

    def __init__(self, limit: int, window_seconds: float, lock_seconds: float, max_keys: int = 20000):
        self.limit, self.window, self.lock = limit, window_seconds, lock_seconds
        self.max_keys = max_keys
        self.items: OrderedDict[str, tuple[list[float], float]] = OrderedDict()  # key -> (моменты, блок до)

    def retry_after(self, key: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        item = self.items.get(key)
        return max(0.0, item[1] - now) if item else 0.0

    def fail(self, key: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        stamps, until = self.items.pop(key, ([], 0.0))
        stamps = [t for t in stamps if now - t < self.window] + [now]
        if len(stamps) >= self.limit:
            until, stamps = now + self.lock, []
        self.items[key] = (stamps, until)
        while len(self.items) > self.max_keys:
            self.items.popitem(last=False)
        return max(0.0, until - now)

    def reset(self, key: str):
        self.items.pop(key, None)


class _BoundedSet:
    def __init__(self, limit: int):
        self.limit, self.items = limit, OrderedDict()

    def add(self, key):
        self.items[key] = None
        self.items.move_to_end(key)
        while len(self.items) > self.limit:
            self.items.popitem(last=False)

    def __contains__(self, key):
        return key in self.items


class EntryGuard:
    """
    Неудачей считается только неверный код. Подключение без кода (например, участник,
    который уже входил на доску) неудачей не считается.

    - handshakes: подключений в минуту с одного адреса (спам входами, в том числе с верным кодом);
    - ip_failures: неверных кодов с одного адреса;
    - board_failures: неверных кодов к одной доске со всех адресов;
    - global_failures: неверных кодов по всей системе (распределённый перебор кодов: вход только
      по коду, поэтому любой подобранный код открывает какую-то доску).
    Пока действует блокировка доски или системы, проходят адреса, которые уже входили успешно.
    """

    def __init__(self, handshakes_per_minute: int, ip_failures: int, board_failures: int,
                 window_seconds: float, lock_seconds: float, global_failures: int = 300):
        self.handshakes = KeyedRateLimiter(handshakes_per_minute)
        self.ip_failures = FailureTracker(ip_failures, window_seconds, lock_seconds)
        self.board_failures = FailureTracker(board_failures, window_seconds, lock_seconds)
        self.global_failures = FailureTracker(global_failures, window_seconds, lock_seconds)
        self.known = _BoundedSet(50000)       # (доска, адрес) с успешным входом
        self.known_ips = _BoundedSet(50000)   # адреса с успешным входом куда-либо

    def allow_handshake(self, ip: str) -> bool:
        return self.handshakes.allow(ip)

    def retry_after(self, ip: str, board_id: str | None = None) -> float:
        wait = self.ip_failures.retry_after(ip)
        if board_id and (board_id, ip) not in self.known:
            wait = max(wait, self.board_failures.retry_after(board_id))
        if ip not in self.known_ips:
            wait = max(wait, self.global_failures.retry_after('*'))
        return wait

    def fail(self, ip: str, board_id: str | None = None):
        self.ip_failures.fail(ip)
        self.global_failures.fail('*')
        if board_id:
            self.board_failures.fail(board_id)

    def success(self, ip: str, board_id: str | None = None):
        self.ip_failures.reset(ip)
        self.known_ips.add(ip)
        if board_id:
            self.known.add((board_id, ip))
