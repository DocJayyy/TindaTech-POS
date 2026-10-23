from django.contrib.auth.decorators import login_required
from accounts.permissions import owner_required

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.shortcuts import render, redirect

from products.models import Product
from .forms import ReceiveStockForm
from .services import receive_stock

from django.views.decorators.http import require_http_methods
from accounts.permissions import owner_required



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

    return render(
        request,
        "inventory/stock_list.html",
        {"products": products},
    )

