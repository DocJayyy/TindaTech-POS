import json
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum, Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

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
    AuditLog,
)
from .services import process_sale
from .utils import log_activity

from django.http import JsonResponse
from django.db.models import Q




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
@require_POST
def complete_sale_view(request):
    form = CheckoutForm(request.POST)

    if not form.is_valid():
        messages.error(request, "Please enter a valid checkout form.")
        return redirect("checkout")

    try:
        cart_items = json.loads(request.POST.get("cart", "[]"))

        if not isinstance(cart_items, list):
            raise ValidationError("Invalid shopping cart.")

        sale = process_sale(
            cart_items=cart_items,
            cashier=request.user,
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
            discount_customer_name=form.cleaned_data[
                "discount_customer_name"
            ],
            discount_reference=form.cleaned_data["discount_reference"],
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
                    "description": form.cleaned_data[
                        "other_charge_description"
                    ],
                },
            ],
        )

    except (
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
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

    log_activity(
        request.user,
        "Sale Completed",
        {
            "sale_id": sale.pk,
            "receipt_number": sale.receipt_number,
            "total_amount": str(sale.total_amount),
            "payment_method": str(sale.payment_method),
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
        .select_related("cashier")
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
            sales = sales.filter(
                created_at__date=selected_date
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

    context = {
        "sales": sales,
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



@login_required
@pos_access_required
def x_reading_view(request):
    # Get the current open shift, if available.
    session = CashierSession.objects.filter(
        cashier=request.user,
        status=CashierSession.Status.OPEN,
    ).order_by("-opened_at").first()

    # Get all saved X Readings belonging to this cashier.
    readings = XReading.objects.filter(
        session__cashier=request.user,
    ).select_related(
        "session",
        "generated_by",
    ).order_by(
        "-generated_at",
    )

    # Default values when there is no open shift.
    context = {
        "session": session,
        "readings": readings,
        "has_open_shift": session is not None,
        "transaction_count": 0,
        "voided_count": 0,
        "total_sales": Decimal("0.00"),
        "cash_sales": Decimal("0.00"),
        "non_cash_sales": Decimal("0.00"),
        "expected_cash": Decimal("0.00"),
    }

    # If there is no open shift, still show the saved history.
    if not session:
        context["has_open_shift"] = False
        return render(
            request,
            "sales/x_reading.html",
            context,
        )

    # Calculate the current shift's sales.
    sales = Sale.objects.filter(cashier_session=session)

    completed_sales = sales.filter(
        status=Sale.Status.COMPLETED,
    )

    voided_sales = sales.filter(
        status=Sale.Status.VOIDED,
    )

    cash_sales = (
        completed_sales.filter(
            payment_method__iexact="cash",
        ).aggregate(
            total=Sum("total_amount"),
        )["total"]
        or Decimal("0.00")
    )

    non_cash_sales = (
        completed_sales.exclude(
            payment_method__iexact="cash",
        ).aggregate(
            total=Sum("total_amount"),
        )["total"]
        or Decimal("0.00")
    )

    total_sales = (
        completed_sales.aggregate(
            total=Sum("total_amount"),
        )["total"]
        or Decimal("0.00")
    )

    transaction_count = completed_sales.count()
    voided_count = voided_sales.count()

    expected_cash = (
        session.opening_cash + cash_sales
    ).quantize(Decimal("0.01"))

    # Save a new X Reading for the current open shift.
    if request.method == "POST":
        try:
            with transaction.atomic():
                last_reading = (
                    XReading.objects.filter(
                        session=session,
                    ).order_by(
                        "-reading_number",
                    ).first()
                )

                reading_number = (
                    last_reading.reading_number + 1
                    if last_reading
                    else 1
                )

                report_data = {
                    "opening_cash": str(session.opening_cash),
                    "total_sales": str(total_sales),
                    "cash_sales": str(cash_sales),
                    "non_cash_sales": str(non_cash_sales),
                    "transaction_count": transaction_count,
                    "voided_count": voided_count,
                    "expected_cash": str(expected_cash),
                }

                reading = XReading.objects.create(
                    session=session,
                    reading_number=reading_number,
                    generated_by=request.user,
                    report_data=report_data,
                )

                log_activity(
                    request.user,
                    "X Reading Generated",
                    {
                        "reading_id": reading.pk,
                        "reading_number": reading.reading_number,
                        "session_id": session.pk,
                        "total_sales": str(total_sales),
                        "cash_sales": str(cash_sales),
                        "non_cash_sales": str(non_cash_sales),
                        "transaction_count": transaction_count,
                        "voided_count": voided_count,
                        "expected_cash": str(expected_cash),
                    },
                )

            messages.success(
                request,
                f"X Reading #{reading.reading_number} "
                "was saved successfully.",
            )

            return redirect("x_reading")

        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Failed to save X Reading for session %s",
                session.pk,
            )

            messages.error(
                request,
                "X Reading could not be saved. "
                "Please check the server error log.",
            )

    # Update the context with the current shift's figures.
    context.update({
        "transaction_count": transaction_count,
        "voided_count": voided_count,
        "total_sales": total_sales,
        "cash_sales": cash_sales,
        "non_cash_sales": non_cash_sales,
        "expected_cash": expected_cash,
    })

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

        expected_cash = (
            session.opening_cash + cash_sales - cash_refunds
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
    )

    # Owners can view all saved Z Readings.
    # Cashiers can only view their own.
    if not is_owner:
        saved_z_readings = saved_z_readings.filter(
            cashier=request.user,
        )

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

        expected_cash = (
            session.opening_cash + cash_sales - cash_refunds
        ).quantize(Decimal("0.01"))

        context = {
            "session": session,
            "is_closed": False,
            "total_sales": total_sales,
            "cash_sales": cash_sales,
            "non_cash_sales": non_cash_sales,
            "cash_refunds": cash_refunds,
            "expected_cash": expected_cash,
            "transaction_count": valid_sales.count(),
            "voided_count": sales.filter(
                status=Sale.Status.VOIDED,
            ).count(),
            "saved_z_readings": saved_z_readings,
        }

    else:
        report = session.z_report_data or {}

        context = {
            "session": session,
            "is_closed": True,
            "report": report,
            "saved_z_readings": saved_z_readings,
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

