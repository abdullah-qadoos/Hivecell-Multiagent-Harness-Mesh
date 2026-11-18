class TodoStore:
    def __init__(self):
        self._items, self._next = {}, 1

    def add(self, title):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must not be empty")
        i, self._next = self._next, self._next + 1
        self._items[i] = {"id": i, "title": title, "done": False}
        return i

    def complete(self, id):
        self._items[id]["done"] = True

    def delete(self, id):
        del self._items[id]

    def list(self, done=None):
        return [dict(t) for t in self._items.values() if done is None or t["done"] == done]
