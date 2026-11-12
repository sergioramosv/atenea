import json
import os
from dataclasses import asdict

from .models import Item


class Store:
    """Inventario persistido en un fichero JSON."""

    def __init__(self, path):
        self.path = path
        self.items = {}
        if os.path.exists(path):
            self.load()

    def load(self):
        with open(self.path) as f:
            data = json.load(f)
        self.items = {d["sku"]: Item(**d) for d in data}

    def save(self):
        with open(self.path, "w") as f:
            json.dump([asdict(i) for i in self.items.values()], f)

    def add(self, sku, name, qty, price):
        if sku in self.items:
            raise ValueError("duplicate sku")
        self.items[sku] = Item(sku, name, qty, price)

    def remove_stock(self, sku, n):
        """Resta n unidades. n debe ser > 0 y no puede dejar stock negativo (ValueError)."""
        item = self.items[sku]
        item.qty -= n

    def total_value(self):
        """Valor total del inventario: suma de qty * price."""
        return sum(i.price for i in self.items.values())

    def low_stock(self, threshold):
        """Artículos con qty <= threshold, ordenados por sku."""
        return [i for i in self.items.values() if i.qty < threshold]
