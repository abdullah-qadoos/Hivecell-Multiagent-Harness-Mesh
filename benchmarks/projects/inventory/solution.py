class Inventory:
    def __init__(self):
        self._stock = {}

    def add(self, sku, qty):
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise ValueError("qty must be a positive int")
        self._stock[sku] = self._stock.get(sku, 0) + qty

    def remove(self, sku, qty):
        if sku not in self._stock:
            raise KeyError(sku)
        if not isinstance(qty, int) or qty <= 0 or qty > self._stock[sku]:
            raise ValueError("invalid or insufficient quantity")
        self._stock[sku] -= qty

    def quantity(self, sku):
        return self._stock.get(sku, 0)

    def low_stock(self, threshold):
        return sorted(s for s, q in self._stock.items() if q < threshold)
