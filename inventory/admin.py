
from django.contrib import admin
from .models import StockBatch, StockMovement


@admin.register(StockBatch)
class StockBatchAdmin(admin.ModelAdmin):
    list_display = [
        "product",
        "batch_code",
        "quantity_received",
        "quantity_remaining",
        "unit_cost",
        "expiration_date",
    ]
    list_filter = ["expiration_date", "supplier"]
    search_fields = ["product__name", "product__barcode", "batch_code"]
    list_per_page = 50
    readonly_fields = [
        "product",
        "supplier",
        "batch_code",
        "quantity_received",
        "quantity_remaining",
        "unit_cost",
        "expiration_date",
        "received_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = [
        "product",
        "movement_type",
        "quantity",
        "reference",
        "created_by",
        "created_at",
    ]
    list_filter = ["movement_type", "created_at"]
    search_fields = ["product__name", "reference"]
    list_per_page = 50
    readonly_fields = [
        "product",
        "batch",
        "movement_type",
        "quantity",
        "reference",
        "notes",
        "created_by",
        "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
