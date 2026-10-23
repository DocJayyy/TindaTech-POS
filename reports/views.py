from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db.models import Sum,F,Count,DecimalField,ExpressionWrapper
from django.db.models.functions import Coalesce
from django.shortcuts import render, redirect
from django.utils import timezone
from accounts.permissions import owner_required
from products.models import Product
from inventory.models import StockBatch
from sales.models import Sale, SaleItem, SaleAdjustment, SaleAdjustmentItem
from datetime import date, datetime, time, timedelta
from django.db.models.functions import TruncDate
import csv
from django.http import HttpResponse
from collections import defaultdict
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.views.decorators.http import require_POST
from sales.models import ReceiptSettings



@login_required
@owner_required
def dashboard_view(request):
    today = timezone.localdate()
    tz = timezone.get_current_timezone()

    # Today's date range
    start = timezone.make_aware(
        datetime.combine(today, time.min),
        tz,
    )
    end = timezone.make_aware(
        datetime.combine(today + timedelta(days=1), time.min),
        tz,
    )

    # Today's completed sales
    today_sales = Sale.objects.filter(
        status=Sale.Status.COMPLETED,
        created_at__gte=start,
        created_at__lt=end,
    )

    # Sales and transaction summary
    sales_summary = today_sales.aggregate(
        total_sales=Coalesce(
            Sum("total_amount"),
            Decimal("0.00"),
            output_field=DecimalField(
                max_digits=24,
                decimal_places=2,
            ),
        ),
        transaction_count=Count("id"),
    )

    today_total_sales = sales_summary["total_sales"]
    transaction_count = sales_summary["transaction_count"]

    # Calculate cost of goods sold
    cost_expression = ExpressionWrapper(
        F("quantity") * F("unit_cost"),
        output_field=DecimalField(
            max_digits=24,
            decimal_places=2,
        ),
    )

    today_cost = SaleItem.objects.filter(
        sale__in=today_sales,
    ).aggregate(
        total_cost=Coalesce(
            Sum(cost_expression),
            Decimal("0.00"),
            output_field=DecimalField(
                max_digits=24,
                decimal_places=2,
            ),
        ),
    )["total_cost"]

    # Estimated gross profit
    estimated_profit = today_total_sales - today_cost

    # Total remaining inventory value at recorded cost
    inventory_value_expression = ExpressionWrapper(
        F("quantity_remaining") * F("unit_cost"),
        output_field=DecimalField(
            max_digits=24,
            decimal_places=2,
        ),
    )

    inventory_value = StockBatch.objects.aggregate(
        total=Coalesce(
            Sum(inventory_value_expression),
            Decimal("0.00"),
            output_field=DecimalField(
                max_digits=24,
                decimal_places=2,
            ),
        ),
    )["total"]

    # Current stock quantity per active product
    products = Product.objects.filter(
        is_active=True,
    ).select_related(
        "category",
    ).annotate(
        stock_quantity=Coalesce(
            Sum("stock_batches__quantity_remaining"),
            Decimal("0.000"),
            output_field=DecimalField(
                max_digits=24,
                decimal_places=3,
            ),
        ),
    )

    # Products at or below their reorder level
    low_stock_products = products.filter(
        stock_quantity__lte=F("reorder_level"),
    ).order_by(
        "stock_quantity",
        "name",
    )

    # Dashboard context
    context = {
        "today_sales": today_total_sales,
        "transaction_count": transaction_count,
        "estimated_profit": estimated_profit,
        "inventory_value": inventory_value,
        "low_stock_count": low_stock_products.count(),
        "low_stock_products": low_stock_products[:10],
    }

    return render(
        request,
        "reports/dashboard.html",
        context,
    )

@login_required
@owner_required
def sales_report_view(request):
    today = timezone.localdate()

    # Select report period.
    period = request.GET.get("period", "daily").lower()
    if period not in ("daily", "weekly", "monthly"):
        period = "daily"

    # Select reference date.
    date_value = request.GET.get("date", str(today))
    try:
        selected_date = date.fromisoformat(date_value)
    except (ValueError, TypeError):
        selected_date = today

    # Calculate date range.
    if period == "weekly":
        start_date = selected_date - timedelta(
            days=selected_date.weekday()
        )
        end_date = start_date + timedelta(days=7)
        period_label = "Weekly Sales"

    elif period == "monthly":
        start_date = selected_date.replace(day=1)

        if start_date.month == 12:
            end_date = date(start_date.year + 1, 1, 1)
        else:
            end_date = date(
                start_date.year,
                start_date.month + 1,
                1,
            )

        period_label = "Monthly Sales"

    else:
        start_date = selected_date
        end_date = start_date + timedelta(days=1)
        period_label = "Daily Sales"

    # Convert local date boundaries to timezone-aware datetimes.
    current_tz = timezone.get_current_timezone()

    start_datetime = timezone.make_aware(
        datetime.combine(start_date, time.min),
        current_tz,
    )
    end_datetime = timezone.make_aware(
        datetime.combine(end_date, time.min),
        current_tz,
    )

    # Only completed sales in the selected period.
    completed_sales = Sale.objects.filter(
        status=Sale.Status.COMPLETED,
        created_at__gte=start_datetime,
        created_at__lt=end_datetime,
    )

    # Original sales revenue.
    sales_summary = completed_sales.aggregate(
        gross_sales=Coalesce(
            Sum("total_amount"),
            Decimal("0.00"),
        ),
        transaction_count=Count("id"),
    )

    # Completed return/refund adjustments linked to these sales.
    completed_adjustments = SaleAdjustment.objects.filter(
        sale__in=completed_sales,
        status="completed",
        adjustment_type__in=("return", "refund"),
    )

    adjustment_summary = completed_adjustments.aggregate(
        total_adjustments=Coalesce(
            Sum("amount"),
            Decimal("0.00"),
        ),
    )

    gross_sales = sales_summary["gross_sales"]
    total_adjustments = adjustment_summary["total_adjustments"]

    # Net sales after returns and refunds.
    total_sales = max(
        Decimal("0.00"),
        gross_sales - total_adjustments,
    )

    # Original cost of the items sold.
    cost_expression = ExpressionWrapper(
        F("quantity") * F("unit_cost"),
        output_field=DecimalField(
            max_digits=24,
            decimal_places=2,
        ),
    )

    original_cost_summary = SaleItem.objects.filter(
        sale__in=completed_sales,
    ).aggregate(
        original_cost=Coalesce(
            Sum(cost_expression),
            Decimal("0.00"),
        ),
    )

    original_cost = original_cost_summary["original_cost"]

    # Cost of returned items that were actually restocked.
    # Use the original SaleItem unit_cost, not today's product cost.
    returned_cost_expression = ExpressionWrapper(
        F("quantity") * F("sale_item__unit_cost"),
        output_field=DecimalField(
            max_digits=24,
            decimal_places=2,
        ),
    )

    restocked_returns = SaleAdjustmentItem.objects.filter(
        adjustment__sale__in=completed_sales,
        adjustment__status="completed",
        adjustment__adjustment_type="return",
        restock=True,
    ).aggregate(
        returned_cost=Coalesce(
            Sum(returned_cost_expression),
            Decimal("0.00"),
        ),
    )

    returned_cost = restocked_returns["returned_cost"]

    # Net COGS after restocked returns.
    total_cost = max(
        Decimal("0.00"),
        original_cost - returned_cost,
    )

    # Gross profit = net sales - net COGS.
    gross_profit = total_sales - total_cost

    context = {
        "period": period,
        "period_label": period_label,
        "selected_date": selected_date,
        "start_date": start_date,
        "end_date": end_date - timedelta(days=1),
        "gross_sales": gross_sales,
        "total_adjustments": total_adjustments,
        "total_sales": total_sales,
        "total_cost": total_cost,
        "gross_profit": gross_profit,
        "transaction_count": sales_summary["transaction_count"],
    }

    return render(
        request,
        "reports/sales_report.html",
        context,
    )


@login_required
@owner_required
def export_sales_report_csv(request):
    import csv
    from datetime import date, datetime, time, timedelta
    from django.http import HttpResponse

    today = timezone.localdate()

    # Validate report period
    period = request.GET.get("period", "daily").lower()
    if period not in ("daily", "weekly", "monthly"):
        period = "daily"

    # Validate selected date
    date_value = request.GET.get("date", str(today))
    try:
        selected_date = date.fromisoformat(date_value)
    except (ValueError, TypeError):
        selected_date = today

    # Calculate date range
    if period == "weekly":
        start_date = selected_date - timedelta(
            days=selected_date.weekday()
        )
        end_date = start_date + timedelta(days=7)

    elif period == "monthly":
        start_date = selected_date.replace(day=1)

        if start_date.month == 12:
            end_date = date(start_date.year + 1, 1, 1)
        else:
            end_date = date(
                start_date.year,
                start_date.month + 1,
                1,
            )

    else:
        start_date = selected_date
        end_date = start_date + timedelta(days=1)

    # Convert local dates to timezone-aware boundaries
    current_tz = timezone.get_current_timezone()

    start_datetime = timezone.make_aware(
        datetime.combine(start_date, time.min),
        current_tz,
    )
    end_datetime = timezone.make_aware(
        datetime.combine(end_date, time.min),
        current_tz,
    )

    # Only completed sales are included
    sales = Sale.objects.filter(
        status=Sale.Status.COMPLETED,
        created_at__gte=start_datetime,
        created_at__lt=end_datetime,
    ).select_related(
        "cashier"
    ).prefetch_related(
        "items"
    ).order_by("created_at")

    # Create downloadable CSV
    response = HttpResponse(
        content_type="text/csv; charset=utf-8",
    )
    response.write("\ufeff")

    filename = f"sales_report_{period}_{selected_date.isoformat()}.csv"
    response["Content-Disposition"] = (
        f'attachment; filename="{filename}"'
    )

    writer = csv.writer(response)

    writer.writerow([
        "Receipt Number",
        "Date",
        "Time",
        "Cashier",
        "Payment Method",
        "Subtotal (PHP)",
        "Discount (PHP)",
        "Sales Total (PHP)",
        "Product Cost (PHP)",
        "Gross Profit (PHP)",
    ])

    for sale in sales:
        product_cost = sum(
            (
                item.quantity * item.unit_cost
                for item in sale.items.all()
            ),
            Decimal("0.00"),
        )

        gross_profit = sale.total_amount - product_cost

        writer.writerow([
            sale.receipt_number,
            timezone.localtime(sale.created_at).strftime("%Y-%m-%d"),
            timezone.localtime(sale.created_at).strftime("%H:%M:%S"),
            sale.cashier.username,
            sale.get_payment_method_display(),
            sale.subtotal,
            sale.discount,
            sale.total_amount,
            product_cost,
            gross_profit,
        ])

    return response


@login_required
@owner_required
def sales_analytics_view(request):
    today = timezone.localdate()
    current_tz = timezone.get_current_timezone()

    # =========================
    # SALES TREND CHART
    # =========================
    try:
        days = int(request.GET.get("days", "7"))
    except (ValueError, TypeError):
        days = 7

    if days not in (7, 30):
        days = 7

    chart_start_date = today - timedelta(days=days - 1)

    chart_start_datetime = timezone.make_aware(
        datetime.combine(chart_start_date, time.min),
        current_tz,
    )
    chart_end_datetime = timezone.make_aware(
        datetime.combine(today + timedelta(days=1), time.min),
        current_tz,
    )

    sales = Sale.objects.filter(
        status=Sale.Status.COMPLETED,
        created_at__gte=chart_start_datetime,
        created_at__lt=chart_end_datetime,
    ).values_list("created_at", "total_amount")

    totals_by_date = defaultdict(Decimal)

    for created_at, total_amount in sales:
        local_day = timezone.localtime(created_at).date()
        totals_by_date[local_day] += (
            total_amount or Decimal("0.00")
        )

    chart_labels = []
    chart_values = []

    for offset in range(days):
        current_date = chart_start_date + timedelta(days=offset)
        chart_labels.append(current_date.strftime("%b %d"))
        chart_values.append(
            float(
                totals_by_date.get(
                    current_date,
                    Decimal("0.00"),
                )
            )
        )

    # =========================
    # BEST-SELLING PRODUCTS
    # =========================
    period = request.GET.get("period", "7days")
    start_date = today - timedelta(days=6)
    end_date = today

    if period == "today":
        start_date = today

    elif period == "month":
        start_date = today.replace(day=1)

    elif period == "custom":
        try:
            start_date = date.fromisoformat(
                request.GET.get("start_date", "")
            )
            end_date = date.fromisoformat(
                request.GET.get("end_date", "")
            )

            if start_date > end_date or end_date > today:
                raise ValueError

        except (ValueError, TypeError):
            start_date = today - timedelta(days=6)
            end_date = today
            period = "7days"

    elif period != "7days":
        period = "7days"
        start_date = today - timedelta(days=6)
        end_date = today

    products_start_datetime = timezone.make_aware(
        datetime.combine(start_date, time.min),
        current_tz,
    )
    products_end_datetime = timezone.make_aware(
        datetime.combine(end_date + timedelta(days=1), time.min),
        current_tz,
    )

    profit_expression = ExpressionWrapper(
        F("line_total") - F("quantity") * F("unit_cost"),
        output_field=DecimalField(
            max_digits=14,
            decimal_places=2,
        ),
    )

    products = (
        SaleItem.objects.filter(
            sale__status=Sale.Status.COMPLETED,
            sale__created_at__gte=products_start_datetime,
            sale__created_at__lt=products_end_datetime,
        )
        .values("product_id", "product__name")
        .annotate(
            quantity_sold=Sum("quantity"),
            sales_revenue=Sum("line_total"),
            gross_profit=Sum(profit_expression),
        )
        .order_by("-quantity_sold", "product__name")
    )

    context = {
        # Chart
        "days": days,
        "chart_labels": chart_labels,
        "chart_values": chart_values,
        "chart_start_date": chart_start_date,
        "today": today,

        # Best-selling products
        "products": products,
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "total_products": products.count(),
    }

    return render(
        request,
        "reports/sales_analytics.html",
        context,
    )



@login_required
def account_settings_view(request):
    if request.method == "POST":
        action = request.POST.get("action")

        # Update username and email
        if action == "update_profile":
            username = request.POST.get("username", "").strip()
            email = request.POST.get("email", "").strip()

            if not username:
                messages.error(request, "Username is required.")

            elif request.user.__class__.objects.filter(
                username__iexact=username
            ).exclude(pk=request.user.pk).exists():
                messages.error(request, "That username is already taken.")

            else:
                request.user.username = username
                request.user.email = email
                request.user.save(
                    update_fields=["username", "email"]
                )

                messages.success(
                    request,
                    "Username and email updated successfully."
                )

        # Change password
        elif action == "change_password":
            current_password = request.POST.get("current_password", "")
            new_password = request.POST.get("new_password", "")
            confirm_password = request.POST.get("confirm_password", "")

            if not request.user.check_password(current_password):
                messages.error(
                    request, "Your current password is incorrect."
                )

            elif new_password != confirm_password:
                messages.error(
                    request, "The new passwords do not match."
                )

            else:
                try:
                    validate_password(new_password, user=request.user)
                except ValidationError as errors:
                    for error in errors.messages:
                        messages.error(request, error)
                else:
                    request.user.set_password(new_password)
                    request.user.save(update_fields=["password"])

                    # Keep user logged in after password change
                    update_session_auth_hash(request, request.user)

                    messages.success(
                        request, "Password changed successfully."
                    )

        else:
            messages.error(request, "Invalid settings action.")

        return redirect("account_settings")

    return render(request, "reports/settings.html")


@login_required
def manage_accounts_view(request):
    User = get_user_model()

    # Owner-only access
    is_owner = (
        request.user.is_superuser
        or request.user.groups.filter(name="Owner").exists()
    )

    if not is_owner:
        messages.error(request, "You are not authorized to manage accounts.")
        return redirect("account_settings")

    cashier_group, _ = Group.objects.get_or_create(name="Cashier")

    if request.method == "POST":
        action = request.POST.get("action")
        user_id = request.POST.get("user_id")

        # Create cashier account
        if action == "create":
            username = request.POST.get("username", "").strip()
            password = request.POST.get("password", "")
            first_name = request.POST.get("first_name", "").strip()
            last_name = request.POST.get("last_name", "").strip()
            email = request.POST.get("email", "").strip()

            if not username or not password:
                messages.error(request, "Username and password are required.")

            elif User.objects.filter(username__iexact=username).exists():
                messages.error(request, "Username is already taken.")

            else:
                new_user = User(
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    is_active=True,
                    is_staff=False,
                )

                try:
                    validate_password(password, user=new_user)
                except ValidationError as errors:
                    for error in errors.messages:
                        messages.error(request, error)
                else:
                    new_user.set_password(password)
                    new_user.save()
                    new_user.groups.add(cashier_group)
                    messages.success(request, "Cashier account created.")

        # Edit cashier details
        elif action == "edit":
            cashier = User.objects.filter(
                pk=user_id, groups__name="Cashier"
            ).first()

            if not cashier:
                messages.error(request, "Cashier account not found.")
            else:
                username = request.POST.get("username", "").strip()
                email = request.POST.get("email", "").strip()
                first_name = request.POST.get("first_name", "").strip()
                last_name = request.POST.get("last_name", "").strip()

                if not username:
                    messages.error(request, "Username is required.")

                elif User.objects.filter(
                    username__iexact=username
                ).exclude(pk=cashier.pk).exists():
                    messages.error(request, "Username is already taken.")

                else:
                    cashier.username = username
                    cashier.email = email
                    cashier.first_name = first_name
                    cashier.last_name = last_name
                    cashier.save()
                    messages.success(request, "Cashier account updated.")

        # Reset cashier password
        elif action == "reset_password":
            cashier = User.objects.filter(
                pk=user_id, groups__name="Cashier"
            ).first()
            new_password = request.POST.get("new_password", "")

            if not cashier:
                messages.error(request, "Cashier account not found.")
            else:
                try:
                    validate_password(new_password, user=cashier)
                except ValidationError as errors:
                    for error in errors.messages:
                        messages.error(request, error)
                else:
                    cashier.set_password(new_password)
                    cashier.save(update_fields=["password"])
                    messages.success(request, "Cashier password reset.")

        # Activate or deactivate cashier
        elif action == "toggle_status":
            cashier = User.objects.filter(
                pk=user_id, groups__name="Cashier"
            ).first()

            if not cashier:
                messages.error(request, "Cashier account not found.")
            else:
                cashier.is_active = not cashier.is_active
                cashier.save(update_fields=["is_active"])

                status = "activated" if cashier.is_active else "deactivated"
                messages.success(request, f"Cashier account {status}.")

        else:
            messages.error(request, "Invalid action.")

        return redirect("manage_accounts")

    cashiers = User.objects.filter(
        groups__name="Cashier"
    ).distinct().order_by("username")

    return render(
        request,
        "reports/manage_accounts.html",
        {
            "cashiers": cashiers,
            "active_cashiers_count": cashiers.filter(is_active=True).count(),
            "inactive_cashiers_count": cashiers.filter(is_active=False).count(),
        },
    )

    
@login_required
def receipt_settings_view(request):
    from sales.models import ReceiptSettings

    is_owner = (
        request.user.is_superuser
        or request.user.groups.filter(name="Owner").exists()
    )

    if not is_owner:
        messages.error(
            request,
            "You are not authorized to manage receipt settings."
        )
        return redirect("account_settings")

    receipt_settings = ReceiptSettings.get_settings()

    if request.method == "POST":
        store_name = request.POST.get("store_name", "").strip()
        store_address = request.POST.get("store_address", "").strip()
        contact_number = request.POST.get("contact_number", "").strip()
        tin_number = request.POST.get("tin_number", "").strip()
        receipt_footer = request.POST.get("receipt_footer", "").strip()
        paper_size = request.POST.get("paper_size", "80")

        if not store_name:
            messages.error(request, "Store name is required.")

        elif paper_size not in ("58", "80"):
            messages.error(request, "Invalid receipt paper size.")

        else:
            receipt_settings.store_name = store_name
            receipt_settings.store_address = store_address
            receipt_settings.contact_number = contact_number
            receipt_settings.tin_number = tin_number
            receipt_settings.receipt_footer = receipt_footer
            receipt_settings.paper_size = paper_size
            receipt_settings.show_contact = (
                request.POST.get("show_contact") == "on"
            )
            receipt_settings.show_tin = (
                request.POST.get("show_tin") == "on"
            )

            receipt_settings.save()

            messages.success(
                request,
                "Receipt settings saved successfully."
            )
            return redirect("receipt_settings")

    return render(
        request,
        "reports/receipt_settings.html",
        {"receipt_settings": receipt_settings},
    )
