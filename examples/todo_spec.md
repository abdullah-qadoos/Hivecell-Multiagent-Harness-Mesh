# Todo store

A small in-memory task tracker library in `services/api/todo.py`.

- `TodoStore.add(title) -> id`; empty or blank titles raise `ValueError`
- `complete(id)`, `delete(id)`; unknown ids raise `KeyError`
- `list(done=None)` returns dicts `{"id", "title", "done"}`, optionally filtered by `done`

Unit tests next to the code, plus black-box tests in `tests/`.
