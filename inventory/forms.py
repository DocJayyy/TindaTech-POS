
from django import forms
from .models import StockBatch, StockMovement


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
