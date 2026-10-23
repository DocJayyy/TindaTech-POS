from decimal import Decimal

from django.db import models
from django.db.models import Sum


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Product(models.Model):
    class Unit(models.TextChoices):
        PIECE = "piece", "Piece"
        PACK = "pack", "Pack"
        BOX = "box", "Box"
        KILOGRAM = "kg", "Kilogram"
        GRAM = "g", "Gram"
        LITER = "liter", "Liter"
        MILLILITER = "ml", "Milliliter"
        BOTTLE = "bottle", "Bottle"
        CAN = "can", "Can"
        SACK = "sack", "Sack"

    barcode = models.CharField(
        max_length=100, unique=True, null=True, blank=True
    )
    sku = models.CharField(
        max_length=50, unique=True, null=True, blank=True
    )
    name = models.CharField(max_length=200, db_index=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="products",
    )
    unit = models.CharField(
        max_length=20,
        choices=Unit.choices,
        default=Unit.PIECE,
    )
    selling_price = models.DecimalField(
        max_digits=12, decimal_places=2
    )
    reorder_level = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=Decimal("5"),
    )
    tracks_expiration = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name", "is_active"]),
        ]

    def __str__(self):
        return self.name

    @property
    def current_stock(self):
        total = self.stock_batches.aggregate(
            total=Sum("quantity_remaining")
        )["total"]
        return total or Decimal("0")

    @property
    def stock_value_at_cost(self):
        from inventory.models import StockBatch

        batches = StockBatch.objects.filter(product=self)
        total = sum(
            (
                batch.quantity_remaining * batch.unit_cost
                for batch in batches.iterator()
            ),
            Decimal("0"),
        )
        return total.quantize(Decimal("0.01"))