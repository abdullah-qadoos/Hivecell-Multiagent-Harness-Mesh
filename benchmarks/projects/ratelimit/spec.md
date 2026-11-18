Token-bucket rate limiter in `services/api/ratelimit.py`, standard library only.

- `TokenBucket(rate, capacity, clock=time.monotonic)`: `rate` tokens per second, at most `capacity` tokens;
  `rate <= 0` or `capacity <= 0` raises `ValueError`
- The bucket starts full and refills continuously over time, never above `capacity`
- `allow(cost=1) -> bool`: if enough tokens, spend them and return `True`; otherwise return `False`
  and spend nothing
- `clock` is a zero-argument function returning seconds, so tests can control time
