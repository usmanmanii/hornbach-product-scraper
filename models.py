"""
models.py – Data models for scraped products.

Uses Python dataclasses for clean serialisation to dicts / Google Sheets rows.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any


@dataclass
class Product:
    """Represents a single product scraped from hornbach.de."""

    name: str = ""
    price: str = ""
    currency: str = "EUR"
    product_url: str = ""
    description: str = ""
    image_url: str = ""
    availability: str = ""
    article_number: str = ""
    category: str = ""
    # Specification table data extracted from product detail page
    specs: Dict[str, str] = field(default_factory=dict)

    # ── Serialisation helpers ────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Convert to a plain dictionary (includes specs flattened)."""
        base = {
            "name": self.name,
            "price": self.price,
            "currency": self.currency,
            "product_url": self.product_url,
            "description": self.description,
            "image_url": self.image_url,
            "availability": self.availability,
            "article_number": self.article_number,
            "category": self.category,
        }
        base.update(self.specs)
        return base

    def to_dict_nested(self) -> dict:
        """Convert to a plain dictionary with specs as a nested dict."""
        return asdict(self)

    def to_row(self) -> List[str]:
        """Convert to a flat list of strings suitable for a Sheets row."""
        return [
            self.name,
            self.price,
            self.currency,
            self.product_url,
            self.description,
            self.image_url,
            self.availability,
            self.article_number,
            self.category,
        ]

    @staticmethod
    def header_row() -> List[str]:
        """Return the header row matching `to_row()` order."""
        return [
            "Product Name",
            "Price",
            "Currency",
            "Product URL",
            "Description",
            "Image URL",
            "Availability",
            "Article Number",
            "Category",
        ]

    def __str__(self) -> str:
        return f"Product({self.name!r}, {self.price} {self.currency}, specs={len(self.specs)})"
