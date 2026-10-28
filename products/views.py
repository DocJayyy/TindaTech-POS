
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.shortcuts import render, redirect, get_object_or_404

from accounts.permissions import owner_required
from sales.utils import log_activity

from .forms import ProductForm
from .models import Product, Category


@login_required
@owner_required
def product_list_view(request):
    search = request.GET.get("q", "").strip()

    products = Product.objects.select_related("category").all()

    if search:
        products = products.filter(
            Q(name__icontains=search)
            | Q(barcode__icontains=search)
            | Q(sku__icontains=search)
            | Q(category__name__icontains=search)
        )

    return render(request, "products/product_list.html", {
        "products": products,
        "search": search,
        "total_products": Product.objects.count(),
        "active_products": Product.objects.filter(is_active=True).count(),
    })


@login_required
@owner_required
def product_create_view(request):
    if request.method == "POST":
        form = ProductForm(request.POST)

        if form.is_valid():
            product = form.save()

            log_activity(
                request.user,
                "Product Created",
                {
                    "product_id": product.pk,
                    "product_name": product.name,
                    "barcode": product.barcode,
                    "sku": product.sku,
                },
            )

            messages.success(
                request,
                f"{product.name} was added successfully."
            )
            return redirect("product_list")
    else:
        form = ProductForm()

    return render(request, "products/product_form.html", {
        "form": form,
        "page_title": "Add Product",
    })


def _product_snapshot(product):
    return {
        "product_name": product.name,
        "barcode": product.barcode,
        "sku": product.sku,
        "category": str(product.category) if product.category else None,
        "is_active": product.is_active,
    }


@login_required
@owner_required
def product_edit_view(request, pk):
    product = get_object_or_404(Product.objects.select_related("category"), pk=pk)

    if request.method == "POST":
        # Take the "before" snapshot FIRST. form.is_valid() already writes the
        # submitted values onto `product` in memory, so reading it afterwards
        # would return the new values instead of the old ones.
        old_values = _product_snapshot(product)

        form = ProductForm(request.POST, instance=product)

        if form.is_valid():
            product = form.save()

            new_values = _product_snapshot(product)

            log_activity(
                request.user,
                "Product Updated",
                {
                    "product_id": product.pk,
                    "old_values": old_values,
                    "new_values": new_values,
                },
            )

            messages.success(
                request,
                f"{product.name} was updated successfully."
            )
            return redirect("product_list")
    else:
        form = ProductForm(instance=product)

    return render(request, "products/product_form.html", {
        "form": form,
        "page_title": "Edit Product",
        "product": product,
    })


    
@login_required
@owner_required
def category_list_view(request):
    categories = Category.objects.all()

    return render(request, "products/category_list.html", {
        "categories": categories,
    })


@login_required
@owner_required
def category_create_view(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()

        if not name:
            messages.error(request, "Category name is required.")

        elif Category.objects.filter(name__iexact=name).exists():
            messages.error(request, "This category already exists.")

        else:
            category = Category.objects.create(
                name=name,
                description=description,
            )

            log_activity(
                request.user,
                "Category Created",
                {
                    "category_id": category.pk,
                    "name": category.name,
                    "description": category.description,
                },
            )

            messages.success(request, "Category added successfully.")
            return redirect("category_list")

    return render(request, "products/category_form.html", {
        "page_title": "Add Category",
        "category": None,
    })


@login_required
@owner_required
def category_edit_view(request, pk):
    category = get_object_or_404(Category, pk=pk)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()

        if not name:
            messages.error(request, "Category name is required.")

        elif Category.objects.filter(
            name__iexact=name
        ).exclude(pk=category.pk).exists():
            messages.error(request, "This category already exists.")

        else:
            old_values = {
                "name": category.name,
                "description": category.description,
            }

            category.name = name
            category.description = description
            category.save()

            log_activity(
                request.user,
                "Category Updated",
                {
                    "category_id": category.pk,
                    "old_values": old_values,
                    "new_values": {
                        "name": category.name,
                        "description": category.description,
                    },
                },
            )

            messages.success(request, "Category updated successfully.")
            return redirect("category_list")

    return render(request, "products/category_form.html", {
        "page_title": "Edit Category",
        "category": category,
    })
