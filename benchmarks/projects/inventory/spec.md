Stock tracker in `services/api/inventory.py`, standard library only.

- `Inventory()`; `add(sku, qty)` adds stock; `qty` must be a positive int, else `ValueError`
- `remove(sku, qty)`: unknown sku raises `KeyError`; removing more than in stock raises `ValueError`
  and changes nothing; a sku that reaches 0 stays known with quantity 0
- `quantity(sku) -> int` (0 for unknown skus)
- `low_stock(threshold) -> list[str]`: known skus with quantity below `threshold`, sorted
