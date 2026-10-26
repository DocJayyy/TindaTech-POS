from django.contrib.auth.decorators import login_required
from accounts.permissions import owner_required

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.shortcuts import render, redirect
from products.models import Product
from .forms import ReceiveStockForm, DamagedStockForm
from .services import receive_stock, record_damaged_stock
from django.views.decorators.http import require_http_methods
from accounts.permissions import owner_required
from datetime import timedelta
from django.db.models import Q
from django.utils import timezone
from .models import StockBatch



@login_required
@owner_required
@require_http_methods(["GET", "POST"])
def receive_stock_view(request):
    if request.method == "POST":
        form = ReceiveStockForm(request.POST)

        if form.is_valid():
            try:
                batch = receive_stock(
                    product=form.cleaned_data["product"],
                    quantity=form.cleaned_data["quantity"],
                    unit_cost=form.cleaned_data["unit_cost"],
                    supplier=form.cleaned_data["supplier"],
                    batch_code=form.cleaned_data["batch_code"],
                    expiration_date=form.cleaned_data["expiration_date"],
                    user=request.user,
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(
                    request,
                    f"Stock received successfully for {batch.product.name}."
                )
                return redirect("receive_stock")
    else:
        form = ReceiveStockForm()

    return render(request, "inventory/receive_stock.html", {
        "form": form,
    })


@login_required
@owner_required
def stock_list_view(request):
    products = (
        Product.objects.filter(is_active=True)
        .select_related("category")
        .annotate(
            stock_quantity=Sum("stock_batches__quantity_remaining")
        )
        .order_by("name")
    )

    low_stock_count = 0
    out_of_stock_count = 0

    for product in products:
        if product.stock_quantity is None:
            product.stock_quantity = 0

        if product.stock_quantity <= 0:
            product.stock_status = "out_of_stock"
            out_of_stock_count += 1
        elif product.stock_quantity <= product.reorder_level:
            product.stock_status = "low_stock"
            low_stock_count += 1
        else:
            product.stock_status = "in_stock"

    return render(
        request,
        "inventory/stock_list.html",
        {
            "products": products,
            "low_stock_count": low_stock_count,
            "out_of_stock_count": out_of_stock_count,
        },
    )
@login_required
@owner_required
def expiration_monitoring_view(request):
    today = timezone.localdate()
    warning_date = today + timedelta(days=30)

    batches = (
        StockBatch.objects
        .filter(quantity_remaining__gt=0)
        .select_related("product")
        .order_by("expiration_date", "product__name")
    )

    search = request.GET.get("search", "").strip()
    status_filter = request.GET.get("status", "")

    if search:
        batches = batches.filter(
            Q(product__name__icontains=search)
            | Q(batch_code__icontains=search)
        )

    expiration_batches = []
    expired_count = 0
    expiring_soon_count = 0
    good_count = 0
    no_expiration_count = 0

    for batch in batches:
        if not batch.expiration_date:
            batch.expiration_status = "no_expiration"
            batch.days_remaining = None
            no_expiration_count += 1

        else:
            days = (batch.expiration_date - today).days
            batch.days_remaining = days

            if days < 0:
                batch.expiration_status = "expired"
                expired_count += 1

            elif batch.expiration_date <= warning_date:
                batch.expiration_status = "expiring_soon"
                expiring_soon_count += 1

            else:
                batch.expiration_status = "good"
                good_count += 1

        if status_filter and batch.expiration_status != status_filter:
            continue

        expiration_batches.append(batch)

    context = {
        "batches": expiration_batches,
        "search": search,
        "status_filter": status_filter,
        "today": today,
        "expired_count": expired_count,
        "expiring_soon_count": expiring_soon_count,
        "good_count": good_count,
        "no_expiration_count": no_expiration_count,
    }

    return render(
        request,
        "inventory/expiration_monitoring.html",
        context,
    )


@login_required
@owner_required
@require_http_methods(["GET", "POST"])
def damaged_stock_view(request):
    if request.method == "POST":
        form = DamagedStockForm(request.POST)

        if form.is_valid():
            try:
                movement = record_damaged_stock(
                    batch_id=form.cleaned_data["batch"].pk,
                    quantity=form.cleaned_data["quantity"],
                    reason=form.cleaned_data["reason"],
                    user=request.user,
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(
                    request,
                    f"Damaged stock recorded for "
                    f"{movement.product.name}.",
                )
                return redirect("damaged_stock")
    else:
        form = DamagedStockForm()

    return render(
        request,
        "inventory/damaged_stock.html",
        {"form": form},
    )
