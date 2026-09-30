"""Необязательные технические таймеры для синхронного worker в POSIX-системах.

SIGALRM ограничивает ожидания вместе с таймаутами сокетов и явными проверками.
Это не изоляция процесса: длительный C-вызов может отложить Python-обработчик.
Обычный socket timeout сбрасывается между чтениями и не ограничивает весь запрос.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import math
import signal
import threading
import time


# No elapsed-time limit for preparing an answer. Transport/DB failure limits remain.
PROCESSING_SECONDS = None
DELIVERY_SECONDS = 60
FAILURE_RECORD_SECONDS = 1
PREPARATION_SECONDS = None
MODEL_SECONDS = 0
PERSISTENCE_RESERVE_SECONDS = 1


class BudgetExpired(BaseException):
    # Не поглощается обработчиками сетевых ошибок и except Exception парсеров.
    pass


_deadline = ContextVar('request_deadline', default=None)


def remaining_seconds(default=None):
    deadline = _deadline.get()
    return default if deadline is None else max(0.0, deadline - time.monotonic())


def check_budget():
    if remaining_seconds(default=1) <= 0:
        raise BudgetExpired()


def request_timeout(maximum):
    check_budget()
    remaining = remaining_seconds()
    if maximum is None:
        return remaining
    return maximum if remaining is None else min(maximum, remaining)


def _alarm(signum, frame):
    raise BudgetExpired()


@contextmanager
def time_limit(seconds):
    if seconds is None:
        check_budget()
        yield
        check_budget()
        return
    if not math.isfinite(seconds) or seconds <= 0:
        raise BudgetExpired()
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, 'setitimer'):
        raise RuntimeError('Бюджет времени требует главного потока worker в POSIX-системах.')
    started = time.monotonic()
    parent = _deadline.get()
    deadline = min(started + seconds, parent) if parent is not None else started + seconds
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if previous_timer[0] and previous_handler is not _alarm:
        raise RuntimeError('В worker уже используется внешний SIGALRM.')
    token = _deadline.set(deadline)
    signal.signal(signal.SIGALRM, _alarm)
    try:
        check_budget()
        signal.setitimer(signal.ITIMER_REAL, max(0.000001, deadline - time.monotonic()))
        yield
        check_budget()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        _deadline.reset(token)
        signal.signal(signal.SIGALRM, previous_handler)
        if parent is not None and previous_handler is _alarm:
            # A child may translate BudgetExpired to a domain error. The parent
            # still needs an alarm, even when its original timer already fired.
            # A millisecond lets this finally block finish restoring its state.
            signal.setitimer(signal.ITIMER_REAL, max(0.001, parent - time.monotonic()))
        elif previous_timer[0]:
            remaining = previous_timer[0] - (time.monotonic() - started)
            if remaining > 0:
                signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])
