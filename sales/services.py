from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, When, Value, IntegerField

from products.models import Product
from inventory.models import StockBatch, StockMovement
from .models import Sale, SaleItem, SaleCharge, CashierSession


CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value):
    return Decimal(str(value)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


@transaction.atomic
def process_sale(
    *,
    cart_items,
    cashier,
    amount_tendered,
    payment_method,
    discount_type="none",
    regular_discount_percent=ZERO,
    special_discount_amount=ZERO,
    discount_customer_name="",
    discount_reference="",
    charges=None,
):
    if not cart_items:
        raise ValidationError("Your cart is empty.")

    # Require an active session for this cashier
    cashier_session = (
        CashierSession.objects
        .select_for_update()
        .filter(
            cashier=cashier,
            status=CashierSession.Status.OPEN,
        )
        .first()
    )

    if cashier_session is None:
        raise ValidationError(
            "You must open your cashier shift before making sales."
        )

    amount_tendered = money(amount_tendered)
    regular_discount_percent = Decimal(
        str(regular_discount_percent or ZERO)
    )
    special_discount_amount = money(
        special_discount_amount or ZERO
    )

    if amount_tendered < ZERO:
        raise ValidationError("Payment cannot be negative.")

    if payment_method not in Sale.PaymentMethod.values:
        raise ValidationError("Invalid payment method.")

    valid_discount_types = Sale.DiscountType.values
    if discount_type not in valid_discount_types:
        raise ValidationError("Invalid discount type.")

    if not ZERO <= regular_discount_percent <= Decimal("100"):
        raise ValidationError("Discount percentage must be 0–100.")

    special_types = {
        Sale.DiscountType.SENIOR,
        Sale.DiscountType.PWD,
        Sale.DiscountType.NAAC,
        Sale.DiscountType.SOLO_PARENT,
    }

    if discount_type in special_types:
        if not discount_customer_name.strip():
            raise ValidationError(
                "Customer name is required for this discount."
            )

        if not discount_reference.strip():
            raise ValidationError(
                "An applicable ID or reference is required."
            )

        if regular_discount_percent != ZERO:
            raise ValidationError(
                "Regular discount percentage cannot be combined "
                "with a special discount."
            )

    elif discount_type == Sale.DiscountType.REGULAR:
        if special_discount_amount != ZERO:
            raise ValidationError(
                "Special discount amount cannot be used "
                "for a regular discount."
            )

    else:
        if regular_discount_percent != ZERO:
            raise ValidationError(
                "Select Regular Discount to apply a percentage."
            )

        if special_discount_amount != ZERO:
            raise ValidationError(
                "Select a special discount to apply an amount."
            )

    quantities = {}

    for item in cart_items:
        try:
            product_id = int(item["product_id"])
            quantity = Decimal(str(item["quantity"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise ValidationError("Invalid product or quantity.")

        if quantity <= ZERO:
            raise ValidationError("Quantity must be greater than zero.")

        if quantity != quantity.quantize(Decimal("0.001")):
            raise ValidationError(
                "Quantity supports up to 3 decimal places."
            )

        quantities[product_id] = (
            quantities.get(product_id, ZERO) + quantity
        )

    products = {
        product.pk: product
        for product in Product.objects.filter(
            pk__in=quantities.keys(),
            is_active=True,
        )
    }

    if len(products) != len(quantities):
        raise ValidationError(
            "One or more products are unavailable. Refresh the cart."
        )

    batches = list(
        StockBatch.objects.filter(
            product_id__in=quantities.keys(),
            quantity_remaining__gt=0,
        )
        .select_for_update()
        .annotate(
            expiration_priority=Case(
                When(expiration_date__isnull=True, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by(
            "product_id",
            "expiration_priority",
            "expiration_date",
            "received_at",
            "pk",
        )
    )

    batches_by_product = {}

    for batch in batches:
        batches_by_product.setdefault(
            batch.product_id, []
        ).append(batch)

    allocations = []
    subtotal = ZERO

    for product_id, requested_quantity in quantities.items():
        product = products[product_id]
        remaining = requested_quantity

        for batch in batches_by_product.get(product_id, []):
            if (
                batch.expiration_date
                and batch.expiration_date < date.today()
            ):
                continue

            allocated = min(
                remaining, batch.quantity_remaining
            )

            if allocated <= ZERO:
                continue

            line_total = money(
                allocated * product.selling_price
            )
            subtotal += line_total

            allocations.append({
                "product": product,
                "batch": batch,
                "quantity": allocated,
                "unit_price": product.selling_price,
                "unit_cost": batch.unit_cost,
                "line_total": line_total,
            })

            remaining -= allocated

            if remaining == ZERO:
                break

        if remaining > ZERO:
            raise ValidationError(
                f"Insufficient stock for {product.name}. "
                f"Short by {remaining} {product.get_unit_display()}."
            )

    subtotal = money(subtotal)

    if discount_type == Sale.DiscountType.REGULAR:
        discount = money(
            subtotal * regular_discount_percent / Decimal("100")
        )
    elif discount_type in special_types:
        discount = special_discount_amount
    else:
        discount = ZERO

    if discount > subtotal:
        raise ValidationError("Discount cannot exceed the subtotal.")

    charge_rows = []
    charge_total = ZERO

    allowed_charge_types = {
        "packaging", "delivery", "service", "other"
    }

    for charge in charges or []:
        try:
            charge_type = charge["charge_type"]
            amount = money(charge["amount"])
            description = str(
                charge.get("description", "")
            ).strip()
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise ValidationError("Invalid additional charge.")

        if charge_type not in allowed_charge_types:
            raise ValidationError("Invalid charge type.")

        if amount < ZERO:
            raise ValidationError("Charges cannot be negative.")

        if amount == ZERO:
            continue

        if charge_type == "other" and not description:
            raise ValidationError(
                "Enter a description for the other charge."
            )

        charge_rows.append({
            "charge_type": charge_type,
            "description": description,
            "amount": amount,
        })

        charge_total += amount

    charge_total = money(charge_total)
    total_amount = money(subtotal - discount + charge_total)

    if amount_tendered < total_amount:
        raise ValidationError(
            f"Payment is insufficient. Amount due: ₱{total_amount}"
        )

    change_amount = money(amount_tendered - total_amount)

    receipt_number = (
        f"POS-{date.today():%Y%m%d}-{uuid4().hex[:8].upper()}"
    )

    sale = Sale.objects.create(
        receipt_number=receipt_number,
        cashier=cashier,
        cashier_session=cashier_session,
        subtotal=subtotal,
        discount=discount,
        discount_type=discount_type,
        discount_rate=(
            regular_discount_percent
            if discount_type == Sale.DiscountType.REGULAR
            else ZERO
        ),
        discount_customer_name=discount_customer_name.strip(),
        discount_reference=discount_reference.strip(),
        additional_charges=charge_total,
        total_amount=total_amount,
        amount_tendered=amount_tendered,
        change_amount=change_amount,
        payment_method=payment_method,
        status=Sale.Status.COMPLETED,
    )

    for row in charge_rows:
        SaleCharge.objects.create(sale=sale, **row)

    for allocation in allocations:
        batch = allocation["batch"]
        quantity = allocation["quantity"]

        batch.quantity_remaining -= quantity
        batch.save(update_fields=["quantity_remaining"])

        SaleItem.objects.create(
            sale=sale,
            product=allocation["product"],
            batch=batch,
            quantity=quantity,
            unit_price=allocation["unit_price"],
            unit_cost=allocation["unit_cost"],
            line_total=allocation["line_total"],
        )

        StockMovement.objects.create(
            product=allocation["product"],
            batch=batch,
            movement_type=StockMovement.MovementType.SALE,
            quantity=quantity,
            reference=receipt_number,
            notes="Stock deducted for completed sale.",
            created_by=cashier,
        )

    return sale
