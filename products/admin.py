
from django.contrib import admin
from .models import Category, Product


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "description"]
    search_fields = ["name"]
    list_per_page = 50


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = [
        "name", "barcode", "sku", "category",
        "unit", "selling_price", "is_active",
    ]
    list_filter = ["category", "unit", "is_active"]
    search_fields = ["name", "barcode", "sku"]
    list_editable = ["selling_price", "is_active"]
    list_per_page = 50
