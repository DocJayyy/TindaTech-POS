from django.contrib import admin
from .models import Supplier


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ["name", "contact_person", "phone", "is_active"]
    search_fields = ["name", "contact_person", "phone"]
    list_filter = ["is_active"]
    list_per_page = 50