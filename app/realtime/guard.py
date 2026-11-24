"""Защита входа на доски: частота подключений, перебор кода с одного адреса и по одной доске."""
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


class EntryGuard:
    """
    - handshakes: подключений в минуту с одного адреса (спам входами, в том числе с верным кодом);
    - ip_failures: неверных кодов с одного адреса по всем доскам;
    - board_failures: неверных кодов к одной доске со всех адресов (распределённый перебор).
      Пока доска заблокирована, войти могут только адреса, которые уже входили на неё успешно.
    """

    def __init__(self, handshakes_per_minute: int, ip_failures: int, board_failures: int,
                 window_seconds: float, lock_seconds: float):
        self.handshakes = KeyedRateLimiter(handshakes_per_minute)
        self.ip_failures = FailureTracker(ip_failures, window_seconds, lock_seconds)
        self.board_failures = FailureTracker(board_failures, window_seconds, lock_seconds)
        self.known: OrderedDict[tuple[str, str], None] = OrderedDict()

    def allow_handshake(self, ip: str) -> bool:
        return self.handshakes.allow(ip)

    def retry_after(self, ip: str, board_id: str) -> float:
        wait = self.ip_failures.retry_after(ip)
        if (board_id, ip) not in self.known:
            wait = max(wait, self.board_failures.retry_after(board_id))
        return wait

    def fail(self, ip: str, board_id: str | None):
        self.ip_failures.fail(ip)
        if board_id:
            self.board_failures.fail(board_id)

    def success(self, ip: str, board_id: str):
        self.ip_failures.reset(ip)
        self.known[(board_id, ip)] = None
        self.known.move_to_end((board_id, ip))
        while len(self.known) > 50000:
            self.known.popitem(last=False)
