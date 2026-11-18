In-memory task tracker in `services/api/todo.py`, standard library only.

- `TodoStore()`; `add(title) -> int` returns a new id; empty or blank titles raise `ValueError`
- `complete(id)` marks it done; `delete(id)` removes it; unknown ids raise `KeyError`
- `list(done=None) -> list[dict]` with keys `id`, `title`, `done`, in insertion order;
  `done=True/False` filters
