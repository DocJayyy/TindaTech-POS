
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q


class Sale(models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        GCASH = "gcash", "GCash"
        CARD = "card", "Debit/Credit Card"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        VOIDED = "voided", "Voided"

    receipt_number = models.CharField(
        max_length=30,
        unique=True,
        blank=True,
    )
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sales",
    )
    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    discount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    amount_tendered = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    change_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
    )
    cashier_session = models.ForeignKey(
    "CashierSession",
    on_delete=models.PROTECT,
    related_name="sales",
    null=True,
    blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.COMPLETED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    

    class DiscountType(models.TextChoices):
        NONE = "none", "No Discount"
        REGULAR = "regular", "Regular Discount"
        SENIOR = "senior", "Senior Citizen"
        PWD = "pwd", "Person with Disability"
        NAAC = "naac", "National Athlete / Coach"
        SOLO_PARENT = "solo_parent", "Solo Parent"

    discount_type = models.CharField(
    max_length=20,
    choices=DiscountType.choices,
    default=DiscountType.NONE,
    )
    discount_rate = models.DecimalField(
    max_digits=5,
    decimal_places=2,
    default=Decimal("0.00"),
    )
    discount_reference = models.CharField(
    max_length=100,
    blank=True,
    )
    discount_customer_name = models.CharField(
    max_length=200,
    blank=True,
    )
    additional_charges = models.DecimalField(
    max_digits=12,
    decimal_places=2,
    default=Decimal("0.00"),
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(discount__gte=0),
                name="sale_discount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(subtotal__gte=0),
                name="sale_subtotal_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=0),
                name="sale_total_nonnegative",
            ),
        ]

    def __str__(self):
        return self.receipt_number or f"Sale #{self.pk}"


class SaleItem(models.Model):
    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        related_name="items",
    )
    product = models.ForeignKey(
        "products.Product",
        on_delete=models.PROTECT,
        related_name="sale_items",
    )
    batch = models.ForeignKey(
        "inventory.StockBatch",
        on_delete=models.PROTECT,
        related_name="sale_items",
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    line_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="sale_item_quantity_positive",
            ),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

    @property
    def profit(self):
        return (
            self.line_total - self.quantity * self.unit_cost
        ).quantize(Decimal("0.01"))

class SaleCharge(models.Model):
    class ChargeType(models.TextChoices):
        PACKAGING = "packaging", "Packaging"
        DELIVERY = "delivery", "Delivery"
        SERVICE = "service", "Service"
        OTHER = "other", "Other"

    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        related_name="charges",
    )
    charge_type = models.CharField(
        max_length=20,
        choices=ChargeType.choices,
    )
    description = models.CharField(max_length=150, blank=True)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="sale_charge_amount_nonnegative",
            ),
        ]

    def __str__(self):
        return f"{self.get_charge_type_display()}: {self.amount}"


class SaleAdjustment(models.Model):
    class AdjustmentType(models.TextChoices):
        VOID = "void", "Void"
        RETURN = "return", "Return"
        REFUND = "refund", "Refund"

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    adjustment_number = models.CharField(
        max_length=40,
        unique=True,
    )

    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        related_name="adjustments",
    )

    adjustment_type = models.CharField(
        max_length=20,
        choices=AdjustmentType.choices,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.COMPLETED,
    )

    reason = models.TextField()

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    reference_receipt_number = models.CharField(
        max_length=30,
    )

    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sale_adjustments",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["adjustment_type", "created_at"]),
            models.Index(fields=["sale", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="sale_adjustment_amount_nonnegative",
            ),
        ]

    def __str__(self):
        return (
            f"{self.adjustment_number} - "
            f"{self.get_adjustment_type_display()}"
        )


class SaleAdjustmentItem(models.Model):
    adjustment = models.ForeignKey(
        SaleAdjustment,
        on_delete=models.PROTECT,
        related_name="items",
    )
    sale_item = models.ForeignKey(
        SaleItem,
        on_delete=models.PROTECT,
        related_name="adjustment_items",
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[
            MinValueValidator(Decimal("0.001"))
        ],
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0.00"))
        ],
    )
    restock = models.BooleanField(default=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="adjustment_item_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="adjustment_item_amount_nonnegative",
            ),
        ]

    def __str__(self):
        return (
            f"{self.sale_item.product.name} "
            f"x {self.quantity}"
        )

class CashierSession(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cashier_sessions",
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.OPEN,
    )

    opening_cash = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    opened_at = models.DateTimeField(auto_now_add=True)

    closed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    # Final Z Reading reconciliation
    expected_cash = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )

    actual_cash = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )

    cash_variance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )

    z_number = models.CharField(
        max_length=40,
        unique=True,
        null=True,
        blank=True,
    )

    # Snapshot of finalized totals
    z_report_data = models.JSONField(
        default=dict,
        blank=True,
    )

    class Meta:
        constraints = [
        models.UniqueConstraint(
            fields=["cashier"],
            condition=Q(status="open"),
            name="one_open_session_per_cashier",
            ),
        ]
        ordering = ["-opened_at"]
        indexes = [
            models.Index(fields=["cashier", "status"]),
            models.Index(fields=["opened_at"]),
        ]

    def __str__(self):
        return (
            f"{self.cashier.username} - "
            f"{self.opened_at:%Y-%m-%d %H:%M} "
            f"({self.status})"
        )


class XReading(models.Model):
    session = models.ForeignKey(
        CashierSession,
        on_delete=models.PROTECT,
        related_name="x_readings",
    )

    reading_number = models.PositiveIntegerField()

    generated_at = models.DateTimeField(
        auto_now_add=True,
    )

    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="generated_x_readings",
    )

    # Snapshot preserves the totals shown at the time of reading
    report_data = models.JSONField(
        default=dict,
        blank=True,
    )

    class Meta:
        ordering = ["-generated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "reading_number"],
                name="unique_x_reading_per_session",
            ),
        ]

    def __str__(self):
        return (
            f"X Reading #{self.reading_number} - "
            f"Session #{self.session_id}"
        )


class ReceiptSettings(models.Model):
    PAPER_SIZE_CHOICES = [
        ("58", "58mm Thermal Paper"),
        ("80", "80mm Thermal Paper"),
    ]

    store_name = models.CharField(
        max_length=150,
        default="GROCERY STORE",
    )
    store_address = models.CharField(
        max_length=255,
        blank=True,
    )
    contact_number = models.CharField(
        max_length=30,
        blank=True,
    )
    tin_number = models.CharField(
        max_length=30,
        blank=True,
    )
    receipt_footer = models.CharField(
        max_length=255,
        default="Thank you for shopping with us!",
    )
    show_contact = models.BooleanField(default=True)
    show_tin = models.BooleanField(default=True)

    paper_size = models.CharField(
        max_length=2,
        choices=PAPER_SIZE_CHOICES,
        default="80",
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Receipt Settings - {self.store_name}"

    @classmethod
    def get_settings(cls):
        settings, _ = cls.objects.get_or_create(pk=1)
        return settings
