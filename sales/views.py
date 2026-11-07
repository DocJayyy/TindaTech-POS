import json
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from uuid import uuid4
from django.core.paginator import Paginator


from datetime import datetime as _datetime, time as _time, timedelta as _timedelta

from django.core.paginator import Paginator
from django.utils import timezone

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum, Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from django.core.paginator import Paginator

from accounts.permissions import (
    pos_access_required,
    owner_required,
)

from products.models import Product

from .adjustment_services import process_adjustment
from .forms import CheckoutForm, SaleAdjustmentForm
from .models import (
    Sale,
    SaleItem,
    SaleAdjustment,
    CashierSession,
    XReading,
    ReceiptSettings,
    RewardSettings,
    RewardAccount,
    AuditLog,
    CashFundMovement,
    SuspendedSale,
    Customer,
    Salesperson,
)
from .services import process_sale
from .utils import log_activity

from django.http import JsonResponse
from django.db.models import Q

# NOTE: put this import at the top of views.py (it used to be inside the
# `except` block). Skip it if you already have it.
import logging



@login_required
@pos_access_required
def checkout_view(request):
    session = CashierSession.objects.filter(
        cashier=request.user,
        status=CashierSession.Status.OPEN,
    ).first()

    if not session:
        messages.warning(
            request,
            "Please open your cashier shift before accessing checkout."
        )
        return redirect("open_shift_page")

    products = Product.objects.filter(
        is_active=True
    ).select_related("category").order_by("name")

    context = {
        "products": products,
        "form": CheckoutForm(),
        "session": session,
        "customers": Customer.objects.all().order_by("name"),
        "salespersons": Salesperson.objects.filter(
            is_active=True
        ).order_by("full_name"),
    }

    is_owner = (
        request.user.is_superuser
        or request.user.groups.filter(name="Owner").exists()
    )

    template_name = (
        "sales/checkout.html"
        if is_owner
        else "sales/cashier_checkout.html"
    )

    return render(request, template_name, context)



@login_required
@pos_access_required
def price_check_view(request):
    query = request.GET.get("q", "").strip()

    if not query:
        return JsonResponse({"products": []})

    products = Product.objects.filter(
        Q(name__icontains=query)
        | Q(barcode__icontains=query)
        | Q(sku__icontains=query),
        is_active=True,
    ).select_related("category")[:20]

    results = [
        {
            "id": product.id,
            "name": product.name,
            "barcode": product.barcode or "",
            "sku": product.sku or "",
            "price": str(product.selling_price),
            "unit": product.get_unit_display(),
        }
        for product in products
    ]

    return JsonResponse({"products": results})


@login_required
@pos_access_required
def inventory_lookup_view(request):
    query = request.GET.get("q", "").strip()

    if not query:
        return JsonResponse({"products": []})

    products = Product.objects.filter(
        Q(name__icontains=query)
        | Q(barcode__icontains=query)
        | Q(sku__icontains=query),
        is_active=True,
    ).select_related("category")[:20]

    results = [
        {
            "id": product.id,
            "name": product.name,
            "barcode": product.barcode or "",
            "sku": product.sku or "",
            "price": str(product.selling_price),
            "unit": product.get_unit_display(),
            "current_stock": str(product.current_stock),
            "reorder_level": str(product.reorder_level),
            "low_stock": (
                product.current_stock <= product.reorder_level
            ),
        }
        for product in products
    ]

    return JsonResponse({"products": results})


@login_required
@pos_access_required
def pricelist_data_view(request):
    products = Product.objects.filter(
        is_active=True
    ).select_related("category")

    results = [
        {
            "id": product.id,
            "name": product.name,
            "sku": product.sku or "",
            "barcode": product.barcode or "",
            "category": product.category.name,
            "unit": product.get_unit_display(),
            "price": str(product.selling_price),
        }
        for product in products
    ]

    return JsonResponse({"products": results})




@login_required
@pos_access_required
@require_POST
def complete_sale_view(request):
    form = CheckoutForm(request.POST)

    if not form.is_valid():
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, error)

        return redirect("checkout")

    try:
        cart_items = json.loads(request.POST.get("cart", "[]"))
        general_note = request.POST.get("general_note", "").strip()[:500]

        # ========================================
        # GET AND VALIDATE SELECTED CUSTOMER
        # ========================================
        customer_id = request.POST.get("customer_id", "").strip()
        customer = None

        if customer_id:
            try:
                customer = Customer.objects.get(pk=int(customer_id))
            except (ValueError, Customer.DoesNotExist):
                raise ValidationError("Selected customer is invalid.")

        # ========================================
        # GET AND VALIDATE SELECTED SALESPERSON
        # ========================================
        salesperson_id = request.POST.get(
            "salesperson_id", ""
        ).strip()

        salesperson = None

        if salesperson_id:
            try:
                salesperson = Salesperson.objects.get(
                    pk=int(salesperson_id),
                    is_active=True,
                )
            except (ValueError, Salesperson.DoesNotExist):
                raise ValidationError(
                    "Selected salesperson is invalid or inactive."
                )

        # ========================================
        # VALIDATE CART
        # ========================================
        if not isinstance(cart_items, list) or not cart_items:
            raise ValidationError(
                "Your shopping cart is empty or invalid."
            )

        # ========================================
        # PROCESS SALE
        # ========================================
        sale = process_sale(
            cart_items=cart_items,
            cashier=request.user,
            customer=customer,
            salesperson=salesperson,
            reward_points_to_redeem=(
                form.cleaned_data["reward_points_to_redeem"] or 0
            ),
            amount_tendered=form.cleaned_data["amount_tendered"],
            payment_method=form.cleaned_data["payment_method"],

            discount_type=form.cleaned_data["discount_type"],
            regular_discount_percent=(
                form.cleaned_data["regular_discount_percent"]
                or Decimal("0")
            ),
            special_discount_amount=(
                form.cleaned_data["special_discount_amount"]
                or Decimal("0")
            ),
            discount_customer_name=(
                form.cleaned_data["discount_customer_name"]
            ),
            discount_reference=(
                form.cleaned_data["discount_reference"]
            ),
            general_note=general_note,

            charges=[
                {
                    "charge_type": "packaging",
                    "amount": (
                        form.cleaned_data["packaging_charge"]
                        or Decimal("0")
                    ),
                },
                {
                    "charge_type": "delivery",
                    "amount": (
                        form.cleaned_data["delivery_charge"]
                        or Decimal("0")
                    ),
                },
                {
                    "charge_type": "service",
                    "amount": (
                        form.cleaned_data["service_charge"]
                        or Decimal("0")
                    ),
                },
                {
                    "charge_type": "other",
                    "amount": (
                        form.cleaned_data["other_charge"]
                        or Decimal("0")
                    ),
                    "description": (
                        form.cleaned_data["other_charge_description"]
                    ),
                },
            ],
        )

    except (
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
        KeyError,
        ArithmeticError,
    ) as exc:

        if isinstance(exc, ValidationError):
            error_message = (
                "; ".join(exc.messages)
                if hasattr(exc, "messages")
                else str(exc)
            )
        else:
            error_message = str(exc)

        messages.error(request, error_message)
        return redirect("checkout")

    # ========================================
    # DELETE SUSPENDED TRANSACTION AFTER SALE
    # ========================================
    suspended_sale_id = request.POST.get("suspended_sale_id")

    if suspended_sale_id:
        cashier_session = CashierSession.objects.filter(
            cashier=request.user,
            status=CashierSession.Status.OPEN,
        ).first()

        if cashier_session:
            SuspendedSale.objects.filter(
                pk=suspended_sale_id,
                cashier=request.user,
                cashier_session=cashier_session,
            ).delete()

    # ========================================
    # RECORD COMPLETED SALE ACTIVITY
    # ========================================
    log_activity(
        request.user,
        "Sale Completed",
        {
            "sale_id": sale.pk,
            "receipt_number": sale.receipt_number,
            "total_amount": str(sale.total_amount),
            "payment_method": str(sale.payment_method),
            "discount_type": str(sale.discount_type),
            "discount_amount": str(sale.discount),
            "reward_points_redeemed": sale.reward_points_redeemed,
            "reward_discount_amount": str(sale.reward_discount_amount),
            "reward_points_earned": sale.reward_points_earned,
        },
    )

    return redirect("sale_receipt", sale_id=sale.pk)


@login_required
@pos_access_required
def sale_receipt_view(request, sale_id):
    from django.shortcuts import get_object_or_404
    from .models import Sale

    sale = get_object_or_404(
        Sale.objects.prefetch_related("items__product"),
        pk=sale_id,
    )

    return render(request, "sales/receipt.html", {
        "sale": sale,
        "receipt_settings": ReceiptSettings.get_settings(),
    })


@login_required
@pos_access_required
def cashier_dashboard_view(request):
    return render(
        request,
        "sales/cashier_dashboard.html",
    )




@login_required
@pos_access_required
def sales_history_view(request):
    is_owner = (
        request.user.is_superuser
        or request.user.groups.filter(name="Owner").exists()
    )

    search = request.GET.get(
        "q",
        request.GET.get("search", ""),
    ).strip()

    template_name = (
        "sales/history.html"
        if is_owner
        else "sales/cashier_history.html"
    )

    # Load sales and their completed adjustments.
    sales = (
        Sale.objects
        .select_related(
            "cashier",
            "salesperson",  # ADDED: Load assigned salesperson
        )
        .prefetch_related(
            "items",
            Prefetch(
                "adjustments",
                queryset=(
                    SaleAdjustment.objects
                    .filter(status="completed")
                    .prefetch_related("items")
                ),
                to_attr="completed_adjustments",
            ),
        )
    )

    # Cashiers can only access their own sales.
    if not is_owner:
        sales = sales.filter(cashier=request.user)

    # Search by receipt number.
    if search:
        sales = sales.filter(
            receipt_number__icontains=search
        )

    # Filter by date.
    selected_date = request.GET.get("date", "").strip()

    if selected_date:
        try:
            selected_date = date.fromisoformat(selected_date)

            # Exact local day: start of day <= created_at < start of next day.
            # More reliable than created_at__date (no DB timezone-table needed).
            day_start = timezone.make_aware(
                _datetime.combine(selected_date, _time.min)
            )
            sales = sales.filter(
                created_at__gte=day_start,
                created_at__lt=day_start + _timedelta(days=1),
            )
        except ValueError:
            selected_date = ""

    # Status filters:
    # all, completed, voided, returns, refunds
    status = request.GET.get("status", "all").strip().lower()

    if status == "completed":
        sales = sales.filter(status=Sale.Status.COMPLETED)

    elif status == "voided":
        sales = sales.filter(status=Sale.Status.VOIDED)

    elif status == "returns":
        sales = sales.filter(
            adjustments__status="completed",
            adjustments__adjustment_type="return",
        ).distinct()

    elif status == "refunds":
        sales = sales.filter(
            adjustments__status="completed",
            adjustments__adjustment_type="refund",
        ).distinct()

    else:
        status = "all"

    sales = list(sales.order_by("-created_at"))

    total_sales = Decimal("0.00")

    for sale in sales:
        # Original quantity sold.
        original_quantity = sum(
            (item.quantity for item in sale.items.all()),
            Decimal("0"),
        )

        returned_quantity = Decimal("0")
        adjustment_total = Decimal("0.00")
        has_void = False

        for adjustment in sale.completed_adjustments:
            adjustment_type = adjustment.adjustment_type

            if adjustment_type == "void":
                has_void = True
                continue

            if adjustment_type in ("return", "refund"):
                adjustment_total += adjustment.amount

            if adjustment_type == "return":
                for adjustment_item in adjustment.items.all():
                    returned_quantity += adjustment_item.quantity

        sale.original_quantity = original_quantity
        sale.returned_quantity = returned_quantity
        sale.net_quantity = max(
            Decimal("0"),
            original_quantity - returned_quantity,
        )

        # Net transaction total.
        if sale.status == Sale.Status.VOIDED or has_void:
            sale.net_total = Decimal("0.00")
        else:
            sale.net_total = max(
                Decimal("0.00"),
                sale.total_amount - adjustment_total,
            )

        total_sales += sale.net_total

    # 10 transactions per page. Totals above still cover ALL filtered sales.
    paginator = Paginator(sales, 10)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Query string without "page", used by the pagination links.
    params = request.GET.copy()
    params.pop("page", None)

    context = {
        "sales": page_obj,
        "qs": params.urlencode(),
        "search": search,
        "search_query": search,
        "selected_date": selected_date,
        "status": status,
        "selected_status": status,
        "is_owner": is_owner,
        "total_sales": total_sales,
        "transaction_count": len(sales),
        "today": date.today(),
    }

    return render(request, template_name, context)


@login_required
@pos_access_required
def sale_adjustment_view(request, sale_id):
    is_owner = (
        request.user.is_superuser
        or request.user.groups.filter(name="Owner").exists()
    )

    if not is_owner:
        raise PermissionDenied(
            "Only the owner can process sales adjustments."
        )

    sale = get_object_or_404(
        Sale.objects.prefetch_related("items__product"),
        pk=sale_id,
    )

    form = (
        SaleAdjustmentForm(request.POST)
        if request.method == "POST"
        else SaleAdjustmentForm()
    )

    form.fields["sale_item_id"].queryset = (
        SaleItem.objects.filter(sale=sale).select_related("product")
    )

    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        adjustment_type = data["adjustment_type"]
        items = None

        if adjustment_type == SaleAdjustment.AdjustmentType.RETURN:
            if not data["sale_item_id"] or not data["return_quantity"]:
                form.add_error(
                    None,
                    "Select a sale item and enter a return quantity.",
                )
            else:
                items = [{
                    "sale_item_id": data["sale_item_id"].pk,
                    "quantity": str(data["return_quantity"]),
                    "restock": data["restock"],
                }]

        if not form.errors:
            try:
                adjustment = process_adjustment(
                    sale_id=sale.pk,
                    adjustment_type=adjustment_type,
                    reason=data["reason"],
                    processed_by=request.user,
                    items=items,
                    refund_amount=data["refund_amount"],
                )
            except ValidationError as exc:
                form.add_error(None, "; ".join(exc.messages))
            else:
                log_activity(
                    request.user,
                    "Sale Adjustment Processed",
                    {
                        "sale_id": sale.pk,
                        "receipt_number": sale.receipt_number,
                        "adjustment_id": adjustment.pk,
                        "adjustment_number": adjustment.adjustment_number,
                        "adjustment_type": str(
                            adjustment.adjustment_type
                        ),
                        "amount": str(adjustment.amount),
                        "reason": adjustment.reason,
                    },
                )

                messages.success(
                    request,
                    f"Adjustment {adjustment.adjustment_number} "
                    "was processed successfully.",
                )
                return redirect("sale_adjustment", sale_id=sale.pk)

    return render(
        request,
        "sales/adjustment.html",
        {
            "sale": sale,
            "form": form,
            "adjustments": sale.adjustments.prefetch_related(
                "items__sale_item__product"
            ),
        },
    )

@login_required
@pos_access_required
def open_shift_page_view(request):
    session = CashierSession.objects.filter(
        cashier=request.user,
        status=CashierSession.Status.OPEN,
    ).first()

    return render(
        request,
        "sales/open_shift.html",
        {"session": session},
    )


@login_required
@pos_access_required
@require_POST
def open_shift_view(request):
    try:
        opening_cash = Decimal(
            request.POST.get("opening_cash", "0").strip()
        )
    except (InvalidOperation, ValueError):
        messages.error(request, "Enter a valid opening cash amount.")
        return redirect("open_shift_page")

    if not opening_cash.is_finite() or opening_cash < Decimal("0.00"):
        messages.error(request, "Enter a valid, non-negative opening cash.")
        return redirect("open_shift_page")

    with transaction.atomic():
        existing_session = (
            CashierSession.objects
            .select_for_update()
            .filter(
                cashier=request.user,
                status=CashierSession.Status.OPEN,
            )
            .first()
        )

        if existing_session:
            messages.warning(request, "You already have an open shift.")
            return redirect("open_shift_page")

        session = CashierSession.objects.create(
            cashier=request.user,
            opening_cash=opening_cash,
            status=CashierSession.Status.OPEN,
        )

    log_activity(
        request.user,
        "Cashier Shift Opened",
        {
            "session_id": session.pk,
            "opening_cash": str(session.opening_cash),
        },
    )

    messages.success(request, "Cashier shift opened successfully!")
    return redirect("checkout")




def get_x_reading_summary(session):
    """Calculate current shift totals for X-Reading."""

    sales = Sale.objects.filter(cashier_session=session)

    completed_sales = sales.filter(
        status=Sale.Status.COMPLETED,
    )

    voided_by_adjustment = SaleAdjustment.objects.filter(
        sale__cashier_session=session,
        status=SaleAdjustment.Status.COMPLETED,
        adjustment_type=SaleAdjustment.AdjustmentType.VOID,
    ).values_list("sale_id", flat=True)

    valid_sales = completed_sales.exclude(
        pk__in=voided_by_adjustment,
    )

    total_sales = (
        valid_sales.aggregate(total=Sum("total_amount"))["total"]
        or Decimal("0.00")
    )

    cash_sales = (
        valid_sales.filter(
            payment_method=Sale.PaymentMethod.CASH,
        ).aggregate(total=Sum("total_amount"))["total"]
        or Decimal("0.00")
    )

    non_cash_sales = (
        valid_sales.exclude(
            payment_method=Sale.PaymentMethod.CASH,
        ).aggregate(total=Sum("total_amount"))["total"]
        or Decimal("0.00")
    )

    cash_refunds = (
        SaleAdjustment.objects.filter(
            sale__cashier_session=session,
            sale__status=Sale.Status.COMPLETED,
            sale__payment_method=Sale.PaymentMethod.CASH,
            status=SaleAdjustment.Status.COMPLETED,
            adjustment_type__in=[
                SaleAdjustment.AdjustmentType.RETURN,
                SaleAdjustment.AdjustmentType.REFUND,
            ],
        )
        .exclude(sale_id__in=voided_by_adjustment)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )

    additional_change_fund = (
        CashFundMovement.objects.filter(
            session=session,
            movement_type=CashFundMovement.MovementType.CASH_IN,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )

    cash_takeout = (
        CashFundMovement.objects.filter(
            session=session,
            movement_type=CashFundMovement.MovementType.CASH_OUT,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )

    transaction_count = valid_sales.count()

    voided_count = sales.filter(
        status=Sale.Status.VOIDED,
    ).count()

    expected_cash = (
        session.opening_cash
        + additional_change_fund
        + cash_sales
        - cash_refunds
        - cash_takeout
    ).quantize(Decimal("0.01"))

    return {
        "opening_cash": session.opening_cash,
        "total_sales": total_sales,
        "cash_sales": cash_sales,
        "non_cash_sales": non_cash_sales,
        "cash_refunds": cash_refunds,
        "additional_change_fund": additional_change_fund,
        "cash_takeout": cash_takeout,
        "transaction_count": transaction_count,
        "voided_count": voided_count,
        "expected_cash": expected_cash,
    }


def create_x_reading(session, user, summary):
    """Save an X-Reading record for the current shift."""

    with transaction.atomic():
        # Lock the session while assigning the next reading number.
        locked_session = CashierSession.objects.select_for_update().get(
            pk=session.pk,
            cashier=user,
            status=CashierSession.Status.OPEN,
        )

        last_reading = XReading.objects.filter(
            session=locked_session,
        ).order_by("-reading_number").first()

        reading_number = (
            last_reading.reading_number + 1
            if last_reading
            else 1
        )

        report_data = {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in summary.items()
        }

        reading = XReading.objects.create(
            session=locked_session,
            reading_number=reading_number,
            generated_by=user,
            report_data=report_data,
        )

        log_activity(
            user,
            "X Reading Generated",
            {
                "reading_id": reading.pk,
                "reading_number": reading.reading_number,
                "session_id": locked_session.pk,
                **report_data,
            },
        )

    return reading


@login_required
@pos_access_required
@require_GET
def x_reading_data_view(request):
    """Return current shift figures for the checkout X-Reading modal."""

    session = CashierSession.objects.filter(
        cashier=request.user,
        status=CashierSession.Status.OPEN,
    ).order_by("-opened_at").first()

    if not session:
        return JsonResponse({
            "success": True,
            "has_open_shift": False,
            "message": "There is no open cashier shift.",
        })

    summary = get_x_reading_summary(session)

    last_reading = XReading.objects.filter(
        session=session,
    ).order_by("-reading_number").first()

    reading_number = (
        last_reading.reading_number + 1
        if last_reading
        else 1
    )

    return JsonResponse({
        "success": True,
        "has_open_shift": True,
        "reading_number": reading_number,
        "reading": {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in summary.items()
        },
    })


@login_required
@pos_access_required
@require_POST
def x_reading_save_view(request):
    """Save X-Reading through the checkout modal."""

    session = CashierSession.objects.filter(
        cashier=request.user,
        status=CashierSession.Status.OPEN,
    ).order_by("-opened_at").first()

    if not session:
        return JsonResponse({
            "success": False,
            "message": "There is no open cashier shift.",
        }, status=400)

    try:
        summary = get_x_reading_summary(session)
        reading = create_x_reading(
            session,
            request.user,
            summary,
        )

        return JsonResponse({
            "success": True,
            "message": (
                f"X-Reading #{reading.reading_number} "
                "was saved successfully."
            ),
            "reading_id": reading.pk,
            "reading_number": reading.reading_number,
        })

    except Exception:
        logger.exception(
            "Failed to save X-Reading for session %s",
            session.pk,
        )

        return JsonResponse({
            "success": False,
            "message": "X-Reading could not be saved. Please check the server error log.",
        }, status=500)


@login_required
@pos_access_required
def x_reading_view(request):
    """Display the full X-Reading page and saved reading history."""

    session = CashierSession.objects.filter(
        cashier=request.user,
        status=CashierSession.Status.OPEN,
    ).order_by("-opened_at").first()

    readings = XReading.objects.filter(
        session__cashier=request.user,
    ).select_related(
        "session",
        "generated_by",
    ).order_by(
        "-generated_at",
        "-id",
    )

    paginator = Paginator(readings, 10)
    readings_page = paginator.get_page(request.GET.get("page"))

    context = {
        "session": session,
        "readings": readings_page,
        "has_open_shift": session is not None,
        "transaction_count": 0,
        "voided_count": 0,
        "total_sales": Decimal("0.00"),
        "cash_sales": Decimal("0.00"),
        "non_cash_sales": Decimal("0.00"),
        "cash_refunds": Decimal("0.00"),
        "additional_change_fund": Decimal("0.00"),
        "cash_takeout": Decimal("0.00"),
        "expected_cash": Decimal("0.00"),
    }

    if session:
        summary = get_x_reading_summary(session)
        context.update(summary)

    if request.method == "POST":
        if not session:
            messages.error(
                request,
                "You need an open cashier shift to generate an X-Reading.",
            )
            return redirect("x_reading")

        try:
            reading = create_x_reading(
                session,
                request.user,
                get_x_reading_summary(session),
            )

            messages.success(
                request,
                f"X-Reading #{reading.reading_number} was saved successfully.",
            )

        except Exception:
            logger.exception(
                "Failed to save X-Reading for session %s",
                session.pk,
            )

            messages.error(
                request,
                "X-Reading could not be saved. Please check the server error log.",
            )

        return redirect("x_reading")

    return render(
        request,
        "sales/x_reading.html",
        context,
    )


@login_required
@pos_access_required
@require_POST
def close_shift_view(request):
    try:
        actual_cash = Decimal(
            request.POST.get("actual_cash", "").strip()
        ).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        messages.error(request, "Enter a valid actual cash amount.")
        return redirect("z_reading")

    if not actual_cash.is_finite() or actual_cash < Decimal("0.00"):
        messages.error(request, "Actual cash cannot be negative.")
        return redirect("z_reading")

    with transaction.atomic():
        session = (
            CashierSession.objects
            .select_for_update()
            .filter(
                cashier=request.user,
                status=CashierSession.Status.OPEN,
            )
            .first()
        )

        if not session:
            messages.error(request, "No open shift found.")
            return redirect("open_shift_page")

        sales = Sale.objects.filter(cashier_session=session)

        completed_sales = sales.filter(
            status=Sale.Status.COMPLETED
        )

        voided_by_adjustment = SaleAdjustment.objects.filter(
            sale__cashier_session=session,
            status=SaleAdjustment.Status.COMPLETED,
            adjustment_type=SaleAdjustment.AdjustmentType.VOID,
        ).values_list("sale_id", flat=True)

        valid_sales = completed_sales.exclude(
            pk__in=voided_by_adjustment
        )

        total_sales = (
            valid_sales.aggregate(total=Sum("total_amount"))["total"]
            or Decimal("0.00")
        )

        cash_sales = (
            valid_sales.filter(
                payment_method=Sale.PaymentMethod.CASH
            ).aggregate(total=Sum("total_amount"))["total"]
            or Decimal("0.00")
        )

        non_cash_sales = (
            valid_sales.exclude(
                payment_method=Sale.PaymentMethod.CASH
            ).aggregate(total=Sum("total_amount"))["total"]
            or Decimal("0.00")
        )

        cash_adjustments = SaleAdjustment.objects.filter(
            sale__cashier_session=session,
            sale__status=Sale.Status.COMPLETED,
            sale__payment_method=Sale.PaymentMethod.CASH,
            status=SaleAdjustment.Status.COMPLETED,
            adjustment_type__in=[
                SaleAdjustment.AdjustmentType.RETURN,
                SaleAdjustment.AdjustmentType.REFUND,
            ],
        ).exclude(sale_id__in=voided_by_adjustment)

        cash_refunds = (
            cash_adjustments.aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )

        # Cash added to the drawer during this shift.
        additional_change_fund = (
            CashFundMovement.objects.filter(
                session=session,
                movement_type=CashFundMovement.MovementType.CASH_IN,
            ).aggregate(
                total=Sum("amount")
            )["total"]
            or Decimal("0.00")
        )

        # Cash removed from the drawer during this shift.
        cash_takeout = (
            CashFundMovement.objects.filter(
                session=session,
                movement_type=CashFundMovement.MovementType.CASH_OUT,
            ).aggregate(
                total=Sum("amount")
            )["total"]
            or Decimal("0.00")
        )

        expected_cash = (
            session.opening_cash
            + additional_change_fund
            + cash_sales
            - cash_refunds
            - cash_takeout
        ).quantize(Decimal("0.01"))

        cash_variance = (
            actual_cash - expected_cash
        ).quantize(Decimal("0.01"))

        completed_count = valid_sales.count()

        voided_count = sales.filter(
            status=Sale.Status.VOIDED
        ).count()

        z_number = f"Z-{session.pk}-{uuid4().hex[:10].upper()}"

        report_data = {
            "cashier": request.user.username,
            "opening_cash": str(session.opening_cash),
            "additional_change_fund": str(additional_change_fund),
            "cash_takeout": str(cash_takeout),
            "total_sales": str(total_sales),
            "cash_sales": str(cash_sales),
            "non_cash_sales": str(non_cash_sales),
            "cash_refunds": str(cash_refunds),
            "expected_cash": str(expected_cash),
            "actual_cash": str(actual_cash),
            "cash_variance": str(cash_variance),
            "completed_transactions": completed_count,
            "voided_transactions": voided_count,
            "closed_at": timezone.localtime().isoformat(),
        }

        session.expected_cash = expected_cash
        session.actual_cash = actual_cash
        session.cash_variance = cash_variance
        session.z_number = z_number
        session.z_report_data = report_data
        session.status = CashierSession.Status.CLOSED
        session.closed_at = timezone.now()

        session.save(update_fields=[
            "expected_cash",
            "actual_cash",
            "cash_variance",
            "z_number",
            "z_report_data",
            "status",
            "closed_at",
        ])

    log_activity(
        request.user,
        "Cashier Shift Closed",
        {
            "session_id": session.pk,
            "z_number": z_number,
            "expected_cash": str(expected_cash),
            "actual_cash": str(actual_cash),
            "cash_variance": str(cash_variance),
            "total_sales": str(total_sales),
            "cash_refunds": str(cash_refunds),
            "additional_change_fund": str(additional_change_fund),
            "cash_takeout": str(cash_takeout),
            "completed_transactions": completed_count,
            "voided_transactions": voided_count,
        },
    )

    log_activity(
        request.user,
        "Z Reading Generated",
        {
            "session_id": session.pk,
            "z_number": z_number,
            "report_data": report_data,
        },
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({
            "success": True,
            "message": f"Shift closed successfully. Z Reading: {z_number}",
            "z_number": z_number,
        })

    messages.success(
        request,
        f"Shift closed successfully. Z Reading: {z_number}",
    )
    return redirect("z_reading")


@login_required
@pos_access_required
def z_reading_view(request):
    # Get the latest session of the logged-in cashier.
    session = CashierSession.objects.filter(
        cashier=request.user,
    ).order_by("-opened_at").first()

    if not session:
        messages.info(
            request,
            "You have not opened a cashier shift yet.",
        )
        return redirect("open_shift_page")

    # Check if the user is an Owner.
    is_owner = (
        request.user.is_superuser
        or request.user.groups.filter(name="Owner").exists()
    )

    # Get all saved Z Readings.
    saved_z_readings = CashierSession.objects.filter(
        status=CashierSession.Status.CLOSED,
        z_number__isnull=False,
        closed_at__isnull=False,
    ).exclude(
        z_report_data={},
    ).select_related(
        "cashier",
    ).order_by(
        "-closed_at",
        "-id",
    )

    # Owners can view all saved Z Readings.
    # Cashiers can only view their own.
    if not is_owner:
        saved_z_readings = saved_z_readings.filter(
            cashier=request.user,
        )

    # Show 10 saved Z Readings per page.
    paginator = Paginator(saved_z_readings, 10)
    saved_z_page = paginator.get_page(request.GET.get("page"))

    # Prepare the current shift report.
    if session.status == CashierSession.Status.OPEN:
        sales = Sale.objects.filter(
            cashier_session=session,
        )

        completed_sales = sales.filter(
            status=Sale.Status.COMPLETED,
        )

        voided_by_adjustment = SaleAdjustment.objects.filter(
            sale__cashier_session=session,
            status=SaleAdjustment.Status.COMPLETED,
            adjustment_type=SaleAdjustment.AdjustmentType.VOID,
        ).values_list("sale_id", flat=True)

        valid_sales = completed_sales.exclude(
            pk__in=voided_by_adjustment,
        )

        total_sales = (
            valid_sales.aggregate(
                total=Sum("total_amount"),
            )["total"]
            or Decimal("0.00")
        )

        cash_sales = (
            valid_sales.filter(
                payment_method=Sale.PaymentMethod.CASH,
            ).aggregate(
                total=Sum("total_amount"),
            )["total"]
            or Decimal("0.00")
        )

        non_cash_sales = (
            valid_sales.exclude(
                payment_method=Sale.PaymentMethod.CASH,
            ).aggregate(
                total=Sum("total_amount"),
            )["total"]
            or Decimal("0.00")
        )

        cash_refunds = (
            SaleAdjustment.objects.filter(
                sale__cashier_session=session,
                sale__status=Sale.Status.COMPLETED,
                sale__payment_method=Sale.PaymentMethod.CASH,
                status=SaleAdjustment.Status.COMPLETED,
                adjustment_type__in=[
                    SaleAdjustment.AdjustmentType.RETURN,
                    SaleAdjustment.AdjustmentType.REFUND,
                ],
            )
            .exclude(
                sale_id__in=voided_by_adjustment,
            )
            .aggregate(
                total=Sum("amount"),
            )["total"]
            or Decimal("0.00")
        )

        # Cash added to the drawer during this shift.
        additional_change_fund = (
            CashFundMovement.objects.filter(
                session=session,
                movement_type=CashFundMovement.MovementType.CASH_IN,
            ).aggregate(
                total=Sum("amount")
            )["total"]
            or Decimal("0.00")
        )

        # Cash removed from the drawer during this shift.
        cash_takeout = (
            CashFundMovement.objects.filter(
                session=session,
                movement_type=CashFundMovement.MovementType.CASH_OUT,
            ).aggregate(
                total=Sum("amount")
            )["total"]
            or Decimal("0.00")
        )

        expected_cash = (
            session.opening_cash
            + additional_change_fund
            + cash_sales
            - cash_refunds
            - cash_takeout
        ).quantize(Decimal("0.01"))

        context = {
            "session": session,
            "is_closed": False,
            "total_sales": total_sales,
            "cash_sales": cash_sales,
            "non_cash_sales": non_cash_sales,
            "cash_refunds": cash_refunds,
            "additional_change_fund": additional_change_fund,
            "cash_takeout": cash_takeout,
            "expected_cash": expected_cash,
            "transaction_count": valid_sales.count(),
            "voided_count": sales.filter(
                status=Sale.Status.VOIDED,
            ).count(),
            "saved_z_readings": saved_z_page,
        }

    else:
        report = session.z_report_data or {}

        context = {
            "session": session,
            "is_closed": True,
            "report": report,
            "saved_z_readings": saved_z_page,
        }

    return render(
        request,
        "sales/z_reading.html",
        context,
    )


@login_required
@pos_access_required
def x_reading_detail_view(request, reading_id):
    reading = get_object_or_404(
        XReading.objects.select_related("session", "generated_by"),
        pk=reading_id,
    )

    # Cashiers can only view readings from their own sessions.
    if not request.user.is_superuser and not request.user.is_staff:
        if reading.session.cashier_id != request.user.id:
            raise Http404("X Reading not found.")

    return render(
        request,
        "sales/x_reading_detail.html",
        {"reading": reading},
    )


@login_required
@pos_access_required
def x_reading_export_pdf_view(request, reading_id):
    from io import BytesIO
    from xhtml2pdf import pisa

    reading = get_object_or_404(
        XReading.objects.select_related("session", "generated_by"),
        pk=reading_id,
    )

    # Cashiers can only export readings from their own sessions.
    if not request.user.is_superuser and not request.user.is_staff:
        if reading.session.cashier_id != request.user.id:
            raise Http404("X Reading not found.")

    html = render_to_string(
        "sales/x_reading_pdf.html",
        {"reading": reading},
    )

    pdf_file = BytesIO()
    result = pisa.CreatePDF(html, dest=pdf_file)

    if result.err:
        return HttpResponse(
            "Unable to generate the X Reading PDF.",
            status=500,
        )

    response = HttpResponse(
        pdf_file.getvalue(),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="X-Reading-{reading.reading_number}.pdf"'
    )
    return response



@login_required
@pos_access_required
def z_reading_detail_view(request, session_id):
    session = get_object_or_404(
        CashierSession.objects.select_related("cashier"),
        pk=session_id,
    )

    is_owner = (
        request.user.is_superuser
        or request.user.groups.filter(name="Owner").exists()
    )

    # Cashiers can only view their own Z Reading.
    if not is_owner and session.cashier_id != request.user.id:
        raise Http404("Z Reading not found.")

    report = session.z_report_data

    # Debug information in the Django terminal.
    print("Z READING DETAIL DEBUG")
    print("Session ID:", session.id)
    print("Session status:", session.status)
    print("Z Number:", session.z_number)
    print("Report data:", report)

    # Only show saved reports for closed shifts.
    if (
        session.status != CashierSession.Status.CLOSED
        or not isinstance(report, dict)
        or not report
    ):
        messages.warning(
            request,
            "No saved Z Reading report found for this shift.",
        )
        return redirect("z_reading")

    context = {
        "session": session,
        "report": report,
    }

    return render(
        request,
        "sales/z_reading_detail.html",
        context,
    )


@login_required
@pos_access_required
def z_reading_export_pdf_view(request, session_id):
    from io import BytesIO
    from xhtml2pdf import pisa

    session = get_object_or_404(
        CashierSession.objects.select_related("cashier"),
        pk=session_id,
    )

    # Check if the logged-in user is the Owner.
    is_owner = (
        request.user.is_superuser
        or request.user.groups.filter(name="Owner").exists()
    )

    # Cashiers can only access their own Z Reading.
    if not is_owner and session.cashier_id != request.user.id:
        raise Http404("Z Reading not found.")

    report = session.z_report_data or {}

    # Only export a saved Z Reading from a closed shift.
    if session.status != CashierSession.Status.CLOSED or not report:
        messages.warning(
            request,
            "No saved Z Reading report found for this shift.",
        )
        return redirect("z_reading")

    # Render the PDF template.
    html = render_to_string(
        "sales/z_reading_pdf.html",
        {
            "session": session,
            "report": report,
        },
    )

    # Generate the PDF file.
    pdf_file = BytesIO()
    result = pisa.CreatePDF(
        html,
        dest=pdf_file,
        encoding="UTF-8",
    )

    if result.err:
        return HttpResponse(
            "Unable to generate the Z Reading PDF. Please try again.",
            status=500,
        )

    # Return the PDF as a downloadable file.
    response = HttpResponse(
        pdf_file.getvalue(),
        content_type="application/pdf",
    )

    filename = session.z_number or f"Z-Reading-{session.pk}"
    response["Content-Disposition"] = (
        f'attachment; filename="{filename}.pdf"'
    )

    return response





@owner_required
def electronic_journal_export(request):
    start_date = request.GET.get("start_date")
    end_date = request.GET.get("end_date")

    sales = Sale.objects.select_related("cashier").order_by("created_at")

    # Filter by start date
    if start_date:
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            start_datetime = timezone.make_aware(
                datetime.combine(start, time.min)
            )
            sales = sales.filter(created_at__gte=start_datetime)
        except ValueError:
            start_date = ""

    # Filter by end date
    if end_date:
        try:
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
            end_datetime = timezone.make_aware(
                datetime.combine(end + timedelta(days=1), time.min)
            )
            sales = sales.filter(created_at__lt=end_datetime)
        except ValueError:
            end_date = ""

    # Count the filtered transactions once
    transaction_count = sales.count()

    response = HttpResponse(
        content_type="text/plain; charset=utf-8"
    )

    filename = (
        f"electronic_journal_{start_date or 'all'}"
        f"_to_{end_date or 'all'}.txt"
    )

    response["Content-Disposition"] = (
        f'attachment; filename="{filename}"'
    )

    response.write("TINDA TECH POS - ELECTRONIC JOURNAL\n")
    response.write("=" * 60 + "\n")
    response.write(
        f"Generated: {timezone.localtime():%Y-%m-%d %H:%M:%S}\n"
    )
    response.write(f"Start date: {start_date or 'All'}\n")
    response.write(f"End date: {end_date or 'All'}\n")
    response.write("=" * 60 + "\n\n")

    if transaction_count == 0:
        response.write("No transactions found.\n")

    for sale in sales.iterator():
        cashier_name = (
            sale.cashier.get_username()
            if sale.cashier else "Unknown"
        )

        created_at = sale.created_at
        if timezone.is_aware(created_at):
            created_at = timezone.localtime(created_at)

        response.write(f"Receipt No.: {sale.receipt_number}\n")
        response.write(f"Date/Time: {created_at:%Y-%m-%d %H:%M:%S}\n")
        response.write(f"Cashier: {cashier_name}\n")
        response.write(f"Subtotal: PHP {sale.subtotal}\n")
        response.write(f"Discount: PHP {sale.discount}\n")
        response.write(f"Total: PHP {sale.total_amount}\n")
        response.write(f"Payment Method: {sale.payment_method}\n")
        response.write(f"Status: {sale.status}\n")
        response.write("-" * 60 + "\n\n")

    # Log once after preparing the journal
    log_activity(
        request.user,
        "Electronic Journal Exported",
        {
            "start_date": start_date or "All",
            "end_date": end_date or "All",
            "transaction_count": transaction_count,
            "filename": filename,
        },
    )

    return response


@owner_required
def electronic_journal_view(request):
    return render(request, "sales/electronic_journal.html")

def is_owner_user(user):
    return (
        user.is_superuser
        or user.groups.filter(name="Owner").exists()
    )

# ---------- CUSTOMER MANAGEMENT ----------

@login_required
@pos_access_required
def customer_list_view(request):
    if not is_owner_user(request.user):
        raise PermissionDenied(
            "Only the owner can manage customers."
        )

    return redirect("checkout")

@login_required
@pos_access_required
def customer_management_data_view(request):
    if not is_owner_user(request.user):
        raise PermissionDenied(
            "Only the owner can manage customers."
        )

    search = request.GET.get("q", "").strip()

    customers = Customer.objects.all().order_by("name")

    if search:
        customers = customers.filter(
            Q(name__icontains=search)
            | Q(contact_number__icontains=search)
        )

    customer_data = [
        {
            "id": customer.id,
            "name": customer.name,
            "contact_number": customer.contact_number,
            "address": customer.address,
            "note": customer.note,
        }
        for customer in customers
    ]

    return JsonResponse({
        "success": True,
        "customers": customer_data,
        "count": len(customer_data),
    })

@login_required
@pos_access_required
def customer_reward_balance_view(request, customer_id):
    customer = Customer.objects.filter(
        pk=customer_id
    ).first()

    if not customer:
        return JsonResponse({
            "success": False,
            "message": "Customer not found.",
        }, status=404)

    reward_settings = RewardSettings.get_settings()

    reward_account = RewardAccount.objects.filter(
        customer=customer
    ).first()

    available_points = (
        reward_account.available_points
        if reward_account else 0
    )

    return JsonResponse({
        "success": True,
        "customer_id": customer.id,
        "customer_name": customer.name,
        "available_points": available_points,
        "is_enabled": reward_settings.is_enabled,
    })

@login_required
@pos_access_required
def salesperson_management_data_view(request):
    if not is_owner_user(request.user):
        raise PermissionDenied(
            "Only the owner can manage salespersons."
        )

    search = request.GET.get("q", "").strip()

    salespersons = Salesperson.objects.all().order_by("full_name")

    if search:
        salespersons = salespersons.filter(
            Q(full_name__icontains=search)
            | Q(contact_number__icontains=search)
        )

    salesperson_data = [
        {
            "id": salesperson.id,
            "full_name": salesperson.full_name,
            "contact_number": salesperson.contact_number,
            "is_active": salesperson.is_active,
        }
        for salesperson in salespersons
    ]

    return JsonResponse({
        "success": True,
        "salespersons": salesperson_data,
        "count": len(salesperson_data),
    })


@login_required
@pos_access_required
@require_POST
def salesperson_create_view(request):
    if not is_owner_user(request.user):
        raise PermissionDenied(
            "Only the owner can add salespersons."
        )

    full_name = request.POST.get("full_name", "").strip()
    contact_number = request.POST.get(
        "contact_number", ""
    ).strip()

    is_ajax = (
        request.headers.get("X-Requested-With")
        == "XMLHttpRequest"
    )

    # Validation
    if not full_name:
        error_message = "Salesperson name is required."

        if is_ajax:
            return JsonResponse({
                "success": False,
                "message": error_message,
            }, status=400)

        messages.error(request, error_message)
        return redirect("checkout")

    if len(full_name) > 200:
        error_message = "Name cannot exceed 200 characters."

        if is_ajax:
            return JsonResponse({
                "success": False,
                "message": error_message,
            }, status=400)

        messages.error(request, error_message)
        return redirect("checkout")

    if len(contact_number) > 30:
        error_message = "Contact number cannot exceed 30 characters."

        if is_ajax:
            return JsonResponse({
                "success": False,
                "message": error_message,
            }, status=400)

        messages.error(request, error_message)
        return redirect("checkout")

    salesperson = Salesperson.objects.create(
        full_name=full_name,
        contact_number=contact_number,
    )

    log_activity(
        request.user,
        "Salesperson Added",
        {
            "salesperson_id": salesperson.pk,
            "salesperson_name": salesperson.full_name,
        },
    )

    if is_ajax:
        return JsonResponse({
            "success": True,
            "message": "Salesperson added successfully.",
            "salesperson": {
                "id": salesperson.pk,
                "full_name": salesperson.full_name,
                "contact_number": salesperson.contact_number,
                "is_active": salesperson.is_active,
            },
        })

    messages.success(
        request,
        "Salesperson added successfully."
    )

    return redirect("checkout")


@login_required
@pos_access_required
@require_POST
def salesperson_update_view(request, salesperson_id):
    if not is_owner_user(request.user):
        raise PermissionDenied(
            "Only the owner can edit salespersons."
        )

    salesperson = get_object_or_404(
        Salesperson,
        pk=salesperson_id,
    )

    full_name = request.POST.get("full_name", "").strip()
    contact_number = request.POST.get(
        "contact_number", ""
    ).strip()

    is_ajax = (
        request.headers.get("X-Requested-With")
        == "XMLHttpRequest"
    )

    # Validation
    if not full_name:
        error_message = "Salesperson name is required."

        if is_ajax:
            return JsonResponse({
                "success": False,
                "message": error_message,
            }, status=400)

        messages.error(request, error_message)
        return redirect("checkout")

    if len(full_name) > 200 or len(contact_number) > 30:
        error_message = "Please check the salesperson information."

        if is_ajax:
            return JsonResponse({
                "success": False,
                "message": error_message,
            }, status=400)

        messages.error(request, error_message)
        return redirect("checkout")

    # Update salesperson
    salesperson.full_name = full_name
    salesperson.contact_number = contact_number
    salesperson.save()

    log_activity(
        request.user,
        "Salesperson Updated",
        {
            "salesperson_id": salesperson.pk,
            "salesperson_name": salesperson.full_name,
        },
    )

    if is_ajax:
        return JsonResponse({
            "success": True,
            "message": "Salesperson updated successfully.",
            "salesperson": {
                "id": salesperson.pk,
                "full_name": salesperson.full_name,
                "contact_number": salesperson.contact_number,
                "is_active": salesperson.is_active,
            },
        })

    messages.success(
        request,
        "Salesperson updated successfully."
    )

    return redirect("checkout")


@login_required
@pos_access_required
@require_POST
def salesperson_toggle_status_view(request, salesperson_id):
    if not is_owner_user(request.user):
        raise PermissionDenied(
            "Only the owner can change salesperson status."
        )

    salesperson = get_object_or_404(
        Salesperson,
        pk=salesperson_id,
    )

    salesperson.is_active = not salesperson.is_active
    salesperson.save(update_fields=["is_active"])

    status = "activated" if salesperson.is_active else "deactivated"

    log_activity(
        request.user,
        f"Salesperson {status.title()}",
        {
            "salesperson_id": salesperson.pk,
            "salesperson_name": salesperson.full_name,
            "is_active": salesperson.is_active,
        },
    )

    return JsonResponse({
        "success": True,
        "message": f"Salesperson {status} successfully.",
        "salesperson": {
            "id": salesperson.pk,
            "full_name": salesperson.full_name,
            "contact_number": salesperson.contact_number,
            "is_active": salesperson.is_active,
        },
    })




@login_required
@pos_access_required
@require_POST
def customer_create_view(request):
    if not is_owner_user(request.user):
        raise PermissionDenied(
            "Only the owner can add customers."
        )

    name = request.POST.get("name", "").strip()
    contact_number = request.POST.get(
        "contact_number", ""
    ).strip()
    address = request.POST.get("address", "").strip()
    note = request.POST.get("note", "").strip()

    # Validation
    if not name:
        error_message = "Customer name is required."

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({
                "success": False,
                "message": error_message,
            }, status=400)

        messages.error(request, error_message)
        return redirect("customer_list")

    if len(name) > 200:
        error_message = "Customer name cannot exceed 200 characters."

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({
                "success": False,
                "message": error_message,
            }, status=400)

        messages.error(request, error_message)
        return redirect("customer_list")

    if len(contact_number) > 30:
        error_message = "Contact number cannot exceed 30 characters."

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({
                "success": False,
                "message": error_message,
            }, status=400)

        messages.error(request, error_message)
        return redirect("customer_list")

    # Create customer
    customer = Customer.objects.create(
        name=name,
        contact_number=contact_number,
        address=address,
        note=note,
    )

    # Activity log
    log_activity(
        request.user,
        "Customer Added",
        {
            "customer_id": customer.pk,
            "customer_name": customer.name,
        },
    )

    # AJAX response
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({
            "success": True,
            "message": "Customer added successfully.",
            "customer": {
                "id": customer.pk,
                "name": customer.name,
                "contact_number": customer.contact_number,
                "address": customer.address,
                "note": customer.note,
            },
        })

    messages.success(
        request,
        "Customer added successfully."
    )

    return redirect("customer_list")


@login_required
@pos_access_required
@require_POST
def customer_update_view(request, customer_id):
    if not is_owner_user(request.user):
        raise PermissionDenied(
            "Only the owner can edit customers."
        )

    customer = get_object_or_404(
        Customer,
        pk=customer_id,
    )

    name = request.POST.get("name", "").strip()
    contact_number = request.POST.get(
        "contact_number", ""
    ).strip()
    address = request.POST.get("address", "").strip()
    note = request.POST.get("note", "").strip()

    is_ajax = (
        request.headers.get("X-Requested-With")
        == "XMLHttpRequest"
    )

    # Validation
    if not name:
        error_message = "Customer name is required."

        if is_ajax:
            return JsonResponse({
                "success": False,
                "message": error_message,
            }, status=400)

        messages.error(request, error_message)
        return redirect("customer_list")

    if len(name) > 200 or len(contact_number) > 30:
        error_message = "Please check the customer information."

        if is_ajax:
            return JsonResponse({
                "success": False,
                "message": error_message,
            }, status=400)

        messages.error(request, error_message)
        return redirect("customer_list")

    # Update customer
    customer.name = name
    customer.contact_number = contact_number
    customer.address = address
    customer.note = note

    customer.save()

    # Activity log
    log_activity(
        request.user,
        "Customer Updated",
        {
            "customer_id": customer.pk,
            "customer_name": customer.name,
        },
    )

    # AJAX response
    if is_ajax:
        return JsonResponse({
            "success": True,
            "message": "Customer information updated successfully.",
            "customer": {
                "id": customer.pk,
                "name": customer.name,
                "contact_number": customer.contact_number,
                "address": customer.address,
                "note": customer.note,
            },
        })

    messages.success(
        request,
        "Customer information updated successfully."
    )

    return redirect("customer_list")

# ---------- SAVE CUSTOMER NOTE ----------

@login_required
@pos_access_required
@require_POST
def customer_note_update_view(request, customer_id):
    customer = get_object_or_404(
        Customer,
        pk=customer_id,
    )

    note = request.POST.get("note", "").strip()

    if len(note) > 2000:
        return JsonResponse({
            "success": False,
            "message": "Customer note cannot exceed 2000 characters.",
        }, status=400)

    customer.note = note
    customer.save(update_fields=["note", "updated_at"])

    log_activity(
        request.user,
        "Customer Note Updated",
        {
            "customer_id": customer.pk,
            "customer_name": customer.name,
        },
    )

    return JsonResponse({
        "success": True,
        "message": "Customer note saved successfully.",
        "note": customer.note,
    })



# ============================================================
# REWARD SETTINGS
# ============================================================

@login_required
@owner_required
def reward_settings_view(request):
    reward_settings = RewardSettings.get_settings()

    if request.method == "POST":
        amount_per_point = request.POST.get(
            "amount_per_point", ""
        ).strip()

        is_enabled = request.POST.get("is_enabled") == "on"

        try:
            amount_per_point = Decimal(amount_per_point)

            if (
                not amount_per_point.is_finite()
                or amount_per_point <= 0
            ):
                raise ValueError

        except (ValueError, TypeError, InvalidOperation):
            messages.error(
                request,
                "Enter a valid earning amount greater than zero.",
            )
            return redirect("reward_settings")

        reward_settings.amount_per_point = amount_per_point
        reward_settings.is_enabled = is_enabled

        reward_settings.save(
            update_fields=[
                "amount_per_point",
                "is_enabled",
                "updated_at",
            ]
        )

        log_activity(
            request.user,
            "Reward Settings Updated",
            {
                "amount_per_point": str(amount_per_point),
                "is_enabled": is_enabled,
            },
        )

        messages.success(
            request,
            "Reward settings saved successfully.",
        )

        return redirect("reward_settings")

    return render(
        request,
        "sales/reward_settings.html",
        {
            "reward_settings": reward_settings,
        },
    )



@login_required
@pos_access_required
@require_GET
def refund_search_view(request):
    if not is_owner_user(request.user):
        return JsonResponse({
            "success": False,
            "message": "Only the owner can process refunds.",
        }, status=403)

    receipt_number = request.GET.get(
        "receipt_number", ""
    ).strip()

    if not receipt_number:
        return JsonResponse({
            "success": False,
            "message": "Enter a receipt number.",
        }, status=400)

    sale = Sale.objects.filter(
        receipt_number__iexact=receipt_number,
        status=Sale.Status.COMPLETED,
    ).first()

    if sale is None:
        return JsonResponse({
            "success": False,
            "message": "Completed receipt not found.",
        }, status=404)

    previous_refunds = SaleAdjustment.objects.filter(
        sale=sale,
        status=SaleAdjustment.Status.COMPLETED,
    ).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0.00")

    remaining = max(
        Decimal("0.00"),
        sale.total_amount - previous_refunds,
    )

    if remaining <= 0:
        return JsonResponse({
            "success": False,
            "message": "This receipt has no remaining refundable balance.",
        }, status=400)

    return JsonResponse({
        "success": True,
        "sale": {
            "id": sale.pk,
            "receipt_number": sale.receipt_number,
            "date": sale.created_at.strftime("%b %d, %Y %I:%M %p"),
            "cashier": sale.cashier.get_full_name()
                or sale.cashier.username,
            "payment_method": sale.get_payment_method_display(),
            "total_amount": str(sale.total_amount),
            "previous_adjustments": str(previous_refunds),
            "remaining_refundable": str(remaining),
        },
    })


@login_required
@pos_access_required
@require_POST
def refund_process_view(request):
    if not is_owner_user(request.user):
        return JsonResponse({
            "success": False,
            "message": "Only the owner can process refunds.",
        }, status=403)

    sale_id = request.POST.get("sale_id")
    refund_amount = request.POST.get("refund_amount")
    reason = request.POST.get("reason", "").strip()

    if not sale_id:
        return JsonResponse({
            "success": False,
            "message": "Invalid receipt.",
        }, status=400)

    try:
        sale = Sale.objects.get(
            pk=sale_id,
            status=Sale.Status.COMPLETED,
        )

        adjustment = process_adjustment(
            sale_id=sale.pk,
            adjustment_type=SaleAdjustment.AdjustmentType.REFUND,
            reason=reason,
            processed_by=request.user,
            refund_amount=refund_amount,
        )

    except Sale.DoesNotExist:
        return JsonResponse({
            "success": False,
            "message": "Completed receipt not found.",
        }, status=404)

    except ValidationError as exc:
        return JsonResponse({
            "success": False,
            "message": " ".join(exc.messages),
        }, status=400)

    log_activity(
        request.user,
        "Sale Refund Processed",
        {
            "sale_id": sale.pk,
            "receipt_number": sale.receipt_number,
            "adjustment_id": adjustment.pk,
            "adjustment_number": adjustment.adjustment_number,
            "amount": str(adjustment.amount),
            "reason": adjustment.reason,
        },
    )

    return JsonResponse({
        "success": True,
        "message": "Refund processed successfully.",
        "adjustment_number": adjustment.adjustment_number,
        "amount": str(adjustment.amount),
    })


@login_required
@pos_access_required
@require_POST
def change_fund_view(request):
    try:
        amount = Decimal(
            request.POST.get("amount", "").strip()
        ).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return JsonResponse({
            "success": False,
            "message": "Enter a valid change fund amount."
        }, status=400)

    reason = request.POST.get("reason", "").strip()

    if not amount.is_finite() or amount <= Decimal("0.00"):
        return JsonResponse({
            "success": False,
            "message": "Amount must be greater than zero."
        }, status=400)

    if not reason:
        return JsonResponse({
            "success": False,
            "message": "Please enter a reason."
        }, status=400)

    if len(reason) > 255:
        return JsonResponse({
            "success": False,
            "message": "Reason cannot exceed 255 characters."
        }, status=400)

    with transaction.atomic():
        session = (
            CashierSession.objects
            .select_for_update()
            .filter(
                cashier=request.user,
                status=CashierSession.Status.OPEN,
            )
            .first()
        )

        if not session:
            return JsonResponse({
                "success": False,
                "message": "You don't have an open cashier shift."
            }, status=400)

        movement = CashFundMovement.objects.create(
            session=session,
            movement_type=CashFundMovement.MovementType.CASH_IN,
            amount=amount,
            reason=reason,
            processed_by=request.user,
        )

        log_activity(
            request.user,
            "Change Fund Added",
            {
                "session_id": session.pk,
                "movement_id": movement.pk,
                "amount": str(amount),
                "reason": reason,
            },
        )

    return JsonResponse({
        "success": True,
        "message": "Change fund added successfully.",
        "amount": str(movement.amount),
    })


@login_required
@pos_access_required
@require_POST
def cash_takeout_view(request):
    try:
        amount = Decimal(
            request.POST.get("amount", "").strip()
        ).quantize(Decimal("0.01"))

    except (InvalidOperation, ValueError, TypeError):
        return JsonResponse({
            "success": False,
            "message": "Enter a valid cash takeout amount."
        }, status=400)

    reason = request.POST.get("reason", "").strip()

    if not amount.is_finite() or amount <= Decimal("0.00"):
        return JsonResponse({
            "success": False,
            "message": "Amount must be greater than zero."
        }, status=400)

    if not reason:
        return JsonResponse({
            "success": False,
            "message": "Please enter a reason."
        }, status=400)

    if len(reason) > 255:
        return JsonResponse({
            "success": False,
            "message": "Reason cannot exceed 255 characters."
        }, status=400)

    with transaction.atomic():
        session = (
            CashierSession.objects
            .select_for_update()
            .filter(
                cashier=request.user,
                status=CashierSession.Status.OPEN,
            )
            .order_by("-opened_at")
            .first()
        )

        if not session:
            return JsonResponse({
                "success": False,
                "message": "You don't have an open cashier shift."
            }, status=400)

        sales = Sale.objects.filter(
            cashier_session=session
        )

        completed_sales = sales.filter(
            status=Sale.Status.COMPLETED
        )

        voided_by_adjustment = SaleAdjustment.objects.filter(
            sale__cashier_session=session,
            status=SaleAdjustment.Status.COMPLETED,
            adjustment_type=SaleAdjustment.AdjustmentType.VOID,
        ).values_list("sale_id", flat=True)

        valid_sales = completed_sales.exclude(
            pk__in=voided_by_adjustment
        )

        cash_sales = (
            valid_sales.filter(
                payment_method=Sale.PaymentMethod.CASH
            ).aggregate(
                total=Sum("total_amount")
            )["total"]
            or Decimal("0.00")
        )

        cash_refunds = (
            SaleAdjustment.objects.filter(
                sale__cashier_session=session,
                sale__status=Sale.Status.COMPLETED,
                sale__payment_method=Sale.PaymentMethod.CASH,
                status=SaleAdjustment.Status.COMPLETED,
                adjustment_type__in=[
                    SaleAdjustment.AdjustmentType.RETURN,
                    SaleAdjustment.AdjustmentType.REFUND,
                ],
            )
            .exclude(
                sale_id__in=voided_by_adjustment
            )
            .aggregate(
                total=Sum("amount")
            )["total"]
            or Decimal("0.00")
        )

        cash_in = (
            CashFundMovement.objects.filter(
                session=session,
                movement_type=CashFundMovement.MovementType.CASH_IN,
            ).aggregate(
                total=Sum("amount")
            )["total"]
            or Decimal("0.00")
        )

        cash_out = (
            CashFundMovement.objects.filter(
                session=session,
                movement_type=CashFundMovement.MovementType.CASH_OUT,
            ).aggregate(
                total=Sum("amount")
            )["total"]
            or Decimal("0.00")
        )

        available_cash = (
            session.opening_cash
            + cash_in
            + cash_sales
            - cash_refunds
            - cash_out
        ).quantize(Decimal("0.01"))

        if amount > available_cash:
            return JsonResponse({
                "success": False,
                "message": (
                    f"Insufficient cash in drawer. "
                    f"Available cash: ₱{available_cash:,.2f}"
                )
            }, status=400)

        movement = CashFundMovement.objects.create(
            session=session,
            movement_type=CashFundMovement.MovementType.CASH_OUT,
            amount=amount,
            reason=reason,
            processed_by=request.user,
        )

    log_activity(
        request.user,
        "Cash Takeout Processed",
        {
            "session_id": session.pk,
            "movement_id": movement.pk,
            "amount": str(amount),
            "reason": reason,
        },
    )

    return JsonResponse({
        "success": True,
        "message": "Cash takeout processed successfully.",
        "amount": str(movement.amount),
        "available_cash": str(
            available_cash - movement.amount
        ),
    })



@login_required
@pos_access_required
@require_POST
def suspend_sale_view(request):
    cashier_session = CashierSession.objects.filter(
        cashier=request.user,
        status=CashierSession.Status.OPEN,
    ).first()

    if not cashier_session:
        return JsonResponse({
            "success": False,
            "message": "Please open your cashier shift first.",
        }, status=400)

    try:
        payload = json.loads(request.body.decode("utf-8"))

        if not isinstance(payload, dict):
            raise ValueError("Invalid transaction data.")

        cart_items = payload.get("cart")

        if not isinstance(cart_items, list) or not cart_items:
            raise ValueError("Your cart is empty.")

        if not all(isinstance(item, dict) for item in cart_items):
            raise ValueError("Invalid cart items.")

        suspended_sale = SuspendedSale.objects.create(
            cashier=request.user,
            cashier_session=cashier_session,
            transaction_data=payload,
        )

        return JsonResponse({
            "success": True,
            "message": "Transaction suspended successfully.",
            "suspended_sale_id": suspended_sale.pk,
        })

    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return JsonResponse({
            "success": False,
            "message": "Invalid transaction data.",
        }, status=400)


@login_required
@pos_access_required
@require_GET
def suspended_sales_list_view(request):
    cashier_session = CashierSession.objects.filter(
        cashier=request.user,
        status=CashierSession.Status.OPEN,
    ).first()

    if not cashier_session:
        return JsonResponse({
            "success": False,
            "message": "Please open your cashier shift first.",
        }, status=400)

    suspended_sales = SuspendedSale.objects.filter(
        cashier=request.user,
        cashier_session=cashier_session,
    ).order_by("-created_at")

    results = []

    for sale in suspended_sales:
        cart_items = sale.transaction_data.get("cart", [])

        results.append({
            "id": sale.pk,
            "items_count": len(cart_items),
            "created_at": sale.created_at.strftime(
                "%b %d, %Y %I:%M %p"
            ),
        })

    return JsonResponse({
        "success": True,
        "suspended_sales": results,
    })


@login_required
@pos_access_required
@require_GET
def retrieve_suspended_sale_view(request, suspended_sale_id):
    cashier_session = CashierSession.objects.filter(
        cashier=request.user,
        status=CashierSession.Status.OPEN,
    ).first()

    if not cashier_session:
        return JsonResponse({
            "success": False,
            "message": "Please open your cashier shift first.",
        }, status=400)

    suspended_sale = SuspendedSale.objects.filter(
        pk=suspended_sale_id,
        cashier=request.user,
        cashier_session=cashier_session,
    ).first()

    if not suspended_sale:
        return JsonResponse({
            "success": False,
            "message": "Suspended transaction not found.",
        }, status=404)

    return JsonResponse({
        "success": True,
        "suspended_sale_id": suspended_sale.pk,
        "transaction_data": suspended_sale.transaction_data,
    })
