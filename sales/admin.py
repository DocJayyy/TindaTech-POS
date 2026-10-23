
from django.contrib import admin
from .models import Sale, SaleItem, SaleAdjustment, SaleAdjustmentItem


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    can_delete = False
    readonly_fields = [
        "product",
        "batch",
        "quantity",
        "unit_price",
        "unit_cost",
        "line_total",
    ]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = [
        "receipt_number",
        "cashier",
        "total_amount",
        "payment_method",
        "status",
        "created_at",
    ]
    list_filter = ["status", "payment_method", "created_at"]
    search_fields = ["receipt_number", "cashier__username"]
    readonly_fields = [
        "receipt_number",
        "cashier",
        "subtotal",
        "discount",
        "total_amount",
        "amount_tendered",
        "change_amount",
        "payment_method",
        "status",
        "created_at",
    ]
    inlines = [SaleItemInline]
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(SaleAdjustment)
class SaleAdjustmentAdmin(admin.ModelAdmin):
    list_display = (
        "adjustment_number",
        "reference_receipt_number",
        "adjustment_type",
        "amount",
        "status",
        "processed_by",
        "created_at",
    )
    list_filter = (
        "adjustment_type",
        "status",
        "created_at",
    )
    search_fields = (
        "adjustment_number",
        "reference_receipt_number",
        "sale__receipt_number",
        "reason",
        "processed_by__username",
    )
    readonly_fields = ("created_at",)


@admin.register(SaleAdjustmentItem)
class SaleAdjustmentItemAdmin(admin.ModelAdmin):
    list_display = (
        "adjustment",
        "sale_item",
        "quantity",
        "amount",
        "restock",
    )
    list_filter = ("restock",)
    search_fields = (
        "adjustment__adjustment_number",
        "adjustment__reference_receipt_number",
        "sale_item__product__name",
    )
