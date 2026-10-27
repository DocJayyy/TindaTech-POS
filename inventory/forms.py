
from django import forms
from .models import StockBatch, StockMovement
from decimal import Decimal


class ReceiveStockForm(forms.Form):
    product = forms.ModelChoiceField(
        queryset=None,
        empty_label="Select a product",
    )
    supplier = forms.ModelChoiceField(
        queryset=None,
        required=False,
        empty_label="Select a supplier (optional)",
    )
    quantity = forms.DecimalField(
        max_digits=12,
        decimal_places=3,
        min_value=0.001,
        label="Quantity received",
    )
    unit_cost = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        label="Cost per unit (₱)",
    )
    batch_code = forms.CharField(
        max_length=100,
        required=False,
    )
    expiration_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        from products.models import Product
        from suppliers.models import Supplier

        self.fields["product"].queryset = Product.objects.filter(
            is_active=True
        ).order_by("name")

        self.fields["supplier"].queryset = Supplier.objects.filter(
            is_active=True
        ).order_by("name")

    def clean(self):
        cleaned_data = super().clean()
        product = cleaned_data.get("product")
        expiration_date = cleaned_data.get("expiration_date")

        if (
            product
            and product.tracks_expiration
            and not expiration_date
        ):
            self.add_error(
                "expiration_date",
                "An expiration date is required for this product.",
            )

        return cleaned_data


from django import forms
from inventory.models import StockBatch


class DamagedStockForm(forms.Form):
    batch = forms.ModelChoiceField(
        queryset=StockBatch.objects.none(),
        label="Product Batch",
    )

    quantity = forms.DecimalField(
        max_digits=12,
        decimal_places=3,
        min_value=Decimal("0.001"),
        label="Damaged Quantity",
    )

    reason = forms.CharField(
        max_length=500,
        label="Reason",
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "Describe why the stock is damaged...",
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["batch"].queryset = (
            StockBatch.objects
            .filter(quantity_remaining__gt=0)
            .select_related("product")
            .order_by("product__name", "expiration_date")
        )


from decimal import Decimal

from django import forms
from django.utils import timezone

from inventory.models import StockBatch


class ExpiredStockForm(forms.Form):
    batch = forms.ModelChoiceField(
        queryset=StockBatch.objects.none(),
        label="Expired Product Batch",
    )

    quantity = forms.DecimalField(
        max_digits=12,
        decimal_places=3,
        min_value=Decimal("0.001"),
        label="Quantity to Remove",
    )

    reason = forms.CharField(
        max_length=500,
        label="Reason",
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "Enter the reason for removing expired stock...",
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["batch"].queryset = (
            StockBatch.objects
            .filter(
                quantity_remaining__gt=0,
                expiration_date__lt=timezone.localdate(),
            )
            .select_related("product")
            .order_by("expiration_date", "product__name")
        )
