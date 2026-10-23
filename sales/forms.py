from decimal import Decimal
from django import forms
from .models import Sale, SaleCharge, SaleAdjustment, SaleItem


class CheckoutForm(forms.Form):
    amount_tendered = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        label="Amount Received (₱)",
        widget=forms.NumberInput(attrs={
            "step": "0.01",
            "min": "0",
        }),
    )

    payment_method = forms.ChoiceField(
        choices=Sale.PaymentMethod.choices,
        initial=Sale.PaymentMethod.CASH,
    )

    discount_type = forms.ChoiceField(
        choices=Sale.DiscountType.choices,
        initial=Sale.DiscountType.NONE,
    )

    # Used for regular discounts only.
    regular_discount_percent = forms.DecimalField(
        required=False,
        initial=Decimal("0"),
        min_value=0,
        max_value=100,
        decimal_places=2,
        max_digits=5,
    )

    # Special discounts require a documented amount until
    # the applicable tax and eligibility calculation is configured.
    special_discount_amount = forms.DecimalField(
        required=False,
        initial=Decimal("0"),
        min_value=0,
        decimal_places=2,
        max_digits=12,
    )

    discount_customer_name = forms.CharField(
        required=False,
        max_length=200,
    )

    discount_reference = forms.CharField(
        required=False,
        max_length=100,
    )

    packaging_charge = forms.DecimalField(
        required=False, initial=Decimal("0"),
        min_value=0, max_digits=12, decimal_places=2,
    )
    delivery_charge = forms.DecimalField(
        required=False, initial=Decimal("0"),
        min_value=0, max_digits=12, decimal_places=2,
    )
    service_charge = forms.DecimalField(
        required=False, initial=Decimal("0"),
        min_value=0, max_digits=12, decimal_places=2,
    )
    other_charge = forms.DecimalField(
        required=False, initial=Decimal("0"),
        min_value=0, max_digits=12, decimal_places=2,
    )

    other_charge_description = forms.CharField(
        required=False, max_length=150,
    )

    def clean(self):
        cleaned = super().clean()
        discount_type = cleaned.get("discount_type")

        special_types = {
            Sale.DiscountType.SENIOR,
            Sale.DiscountType.PWD,
            Sale.DiscountType.NAAC,
            Sale.DiscountType.SOLO_PARENT,
        }

        if discount_type == Sale.DiscountType.REGULAR:
            if cleaned.get("special_discount_amount") not in (
                None, Decimal("0")
            ):
                self.add_error(
                    "special_discount_amount",
                    "Use the regular discount percentage instead.",
                )

        elif discount_type in special_types:
            if cleaned.get("regular_discount_percent") not in (
                None, Decimal("0")
            ):
                self.add_error(
                    "regular_discount_percent",
                    "Regular discount percentage cannot be used here.",
                )

            if not cleaned.get("discount_customer_name"):
                self.add_error(
                    "discount_customer_name",
                    "Enter the eligible customer's name.",
                )

            if not cleaned.get("discount_reference"):
                self.add_error(
                    "discount_reference",
                    "Enter the applicable ID or reference number.",
                )

        else:
            if cleaned.get("regular_discount_percent") not in (
                None, Decimal("0")
            ):
                self.add_error(
                    "regular_discount_percent",
                    "Select Regular Discount first.",
                )

            if cleaned.get("special_discount_amount") not in (
                None, Decimal("0")
            ):
                self.add_error(
                    "special_discount_amount",
                    "Select a special discount first.",
                )

        return cleaned

        
class SaleAdjustmentForm(forms.Form):
    adjustment_type = forms.ChoiceField(
        choices=SaleAdjustment.AdjustmentType.choices,
        label="Adjustment Type",
    )

    reason = forms.CharField(
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "Explain why this adjustment is needed",
        }),
        max_length=1000,
        label="Reason",
    )

    refund_amount = forms.DecimalField(
        required=False,
        min_value=0.01,
        max_digits=12,
        decimal_places=2,
        label="Refund Amount (₱)",
        widget=forms.NumberInput(attrs={
            "step": "0.01",
            "min": "0.01",
            "placeholder": "0.00",
        }),
    )


    sale_item_id = forms.ModelChoiceField(
        queryset=SaleItem.objects.none(),
        required=False,
        empty_label="Select a product from the receipt",
        label="Product to Return",
        widget=forms.Select(attrs={
            "class": "w-full rounded-lg border border-gray-300 p-3"
        }),
    )


    return_quantity = forms.DecimalField(
        required=False,
        min_value=0.001,
        max_digits=12,
        decimal_places=3,
        label="Return Quantity",
        widget=forms.NumberInput(attrs={
            "step": "0.001",
            "min": "0.001",
        }),
    )

    restock = forms.BooleanField(
        required=False,
        initial=True,
        label="Add returned items back to stock",
    )
