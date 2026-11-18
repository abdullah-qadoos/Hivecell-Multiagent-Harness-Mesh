import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent / "services" / "api"))
from ratelimit import TokenBucket  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_burst_then_refill():
    clock = Clock()
    b = TokenBucket(rate=2, capacity=3, clock=clock)
    assert [b.allow() for _ in range(4)] == [True, True, True, False]
    clock.t += 0.5  # +1 token
    assert b.allow() and not b.allow()


def test_never_above_capacity_and_cost():
    clock = Clock()
    b = TokenBucket(rate=4, capacity=2, clock=clock)
    clock.t += 100
    assert b.allow(2) and not b.allow(1)
    assert not b.allow(5)  # more than capacity: refused, spends nothing
    clock.t += 0.25  # exactly 1 token (0.25 is exact in binary floating point)
    assert b.allow(1)


def test_validation():
    for rate, cap in ((0, 1), (1, 0), (-1, 5)):
        with pytest.raises(ValueError):
            TokenBucket(rate, cap)
