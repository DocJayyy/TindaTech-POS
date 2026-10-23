from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q


class StockBatch(models.Model):
    product = models.ForeignKey(
        "products.Product",
        on_delete=models.PROTECT,
        related_name="stock_batches",
    )
    supplier = models.ForeignKey(
        "suppliers.Supplier",
        on_delete=models.PROTECT,
        related_name="stock_batches",
        null=True,
        blank=True,
    )
    batch_code = models.CharField(max_length=100, blank=True)
    quantity_received = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    quantity_remaining = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0"))],
    )
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    expiration_date = models.DateField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["expiration_date", "received_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity_received__gt=0),
                name="batch_received_positive",
            ),
            models.CheckConstraint(
                condition=Q(quantity_remaining__gte=0),
                name="batch_remaining_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(
                    quantity_remaining__lte=F("quantity_received")
                ),
                name="batch_remaining_not_above_received",
            ),
        ]
        indexes = [
            models.Index(fields=["product", "expiration_date"]),
        ]

    def __str__(self):
        return f"{self.product.name} - Batch {self.pk}"


class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        STOCK_IN = "stock_in", "Stock In"
        SALE = "sale", "Sale"
        SALE_RETURN = "sale_return", "Sale Return"
        ADJUSTMENT_IN = "adjustment_in", "Adjustment In"
        ADJUSTMENT_OUT = "adjustment_out", "Adjustment Out"
        DAMAGED = "damaged", "Damaged"
        EXPIRED = "expired", "Expired"

    product = models.ForeignKey(
        "products.Product",
        on_delete=models.PROTECT,
        related_name="stock_movements",
    )
    batch = models.ForeignKey(
        StockBatch,
        on_delete=models.PROTECT,
        related_name="movements",
        null=True,
        blank=True,
    )
    movement_type = models.CharField(
        max_length=20,
        choices=MovementType.choices,
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["product", "created_at"]),
            models.Index(fields=["movement_type", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="movement_quantity_positive",
            ),
        ]

    def __str__(self):
        return f"{self.get_movement_type_display()} - {self.product.name}"