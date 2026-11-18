LRU cache in `services/api/lru.py`, standard library only.

- `LRUCache(capacity)`; capacity below 1 raises `ValueError`
- `get(key, default=None)` returns the value and marks the key as most recently used
- `put(key, value)` inserts or updates (an update also marks it most recently used);
  when full, evicts the least recently used key
- `len(cache)` is the number of keys
