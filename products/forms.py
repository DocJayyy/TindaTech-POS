
from django import forms
from .models import Product


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "barcode",
            "sku",
            "category",
            "unit",
            "selling_price",
            "reorder_level",
            "tracks_expiration",
            "is_active",
        ]

        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-input",
                "placeholder": "Enter product name",
            }),
            "barcode": forms.TextInput(attrs={
                "class": "form-input",
                "placeholder": "Scan or enter barcode",
            }),
            "sku": forms.TextInput(attrs={
                "class": "form-input",
                "placeholder": "Enter SKU (optional)",
            }),
            "selling_price": forms.NumberInput(attrs={
                "class": "form-input",
                "step": "0.01",
                "min": "0",
                "placeholder": "0.00",
            }),
            "reorder_level": forms.NumberInput(attrs={
                "class": "form-input",
                "step": "0.001",
                "min": "0",
            }),
        }

    def clean_barcode(self):
        barcode = self.cleaned_data.get("barcode")
        return barcode or None

    def clean_sku(self):
        sku = self.cleaned_data.get("sku")
        return sku or None
