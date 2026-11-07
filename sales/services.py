from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, When, Value, IntegerField, Q
from django.utils import timezone

from products.models import Product
from inventory.models import StockBatch, StockMovement
from .models import (
Sale,
SaleItem,
SaleCharge,
CashierSession,
RewardSettings,
RewardAccount,
RewardTransaction,
Salesperson,
)


CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value):
    return Decimal(str(value)).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )


@transaction.atomic
def process_sale(
    *,
    cart_items,
    cashier,
    customer=None,
    salesperson=None,
    amount_tendered,
    payment_method,
    discount_type="none",
    regular_discount_percent=ZERO,
    special_discount_amount=ZERO,
    discount_customer_name="",
    discount_reference="",
    general_note="",
    charges=None,
    reward_points_to_redeem=0,
):
    if not cart_items:
        raise ValidationError("Your cart is empty.")

    # Require an active cashier shift.
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

    # ========================================
    # VALIDATE ASSIGNED SALESPERSON
    # ========================================
    if salesperson is not None:
        if not salesperson.is_active:
            raise ValidationError(
                "Selected salesperson is inactive."
            )

    today = timezone.localdate()

    try:
        amount_tendered = money(amount_tendered)
        regular_discount_percent = Decimal(
            str(regular_discount_percent or ZERO)
        )
        special_discount_amount = money(
            special_discount_amount or ZERO
        )
    except (TypeError, ValueError, ArithmeticError):
        raise ValidationError("Invalid payment or discount amount.")

    if not amount_tendered.is_finite():
        raise ValidationError("Invalid payment amount.")

    if not regular_discount_percent.is_finite():
        raise ValidationError("Invalid discount percentage.")

    if not special_discount_amount.is_finite():
        raise ValidationError("Invalid special discount amount.")

    if special_discount_amount < ZERO:
        raise ValidationError("Special discount cannot be negative.")

    if amount_tendered < ZERO:
        raise ValidationError("Payment cannot be negative.")

    if payment_method not in Sale.PaymentMethod.values:
        raise ValidationError("Invalid payment method.")

    if discount_type not in Sale.DiscountType.values:
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

    # Combine duplicate products and validate quantities.
    quantities = {}

    for item in cart_items:
        try:
            product_id = int(item["product_id"])
            quantity = Decimal(str(item["quantity"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise ValidationError("Invalid product or quantity.")

        if not quantity.is_finite() or quantity <= ZERO:
            raise ValidationError(
                "Quantity must be a valid number greater than zero."
            )

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

    # Only use available stock that has not expired.
    # Products with no expiration date remain eligible.
    batches = list(
        StockBatch.objects.filter(
            product_id__in=quantities.keys(),
            quantity_remaining__gt=ZERO,
        )
        .filter(
            Q(expiration_date__isnull=True)
            | Q(expiration_date__gte=today)
        )
        .select_for_update()
        .annotate(
            expiration_priority=Case(
                When(
                    expiration_date__isnull=True,
                    then=Value(1),
                ),
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

    # Allocate stock using earliest-expiring batches first.
    allocations = []
    subtotal = ZERO

    for product_id, requested_quantity in quantities.items():
        product = products[product_id]
        remaining = requested_quantity

        for batch in batches_by_product.get(product_id, []):
            # Extra safeguard against expired batches.
            if (
                batch.expiration_date is not None
                and batch.expiration_date < today
            ):
                continue

            allocated = min(
                remaining,
                batch.quantity_remaining,
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
                f"Insufficient valid stock for {product.name}. "
                f"Short by {remaining} {product.get_unit_display()}. "
                "Expired stock cannot be sold."
            )

    subtotal = money(subtotal)

    # Calculate discount.
    if discount_type == Sale.DiscountType.REGULAR:
        discount = money(
            subtotal
            * regular_discount_percent
            / Decimal("100")
        )

    elif discount_type in special_types:
        discount = money(special_discount_amount)

    else:
        discount = ZERO

    if discount < ZERO:
        raise ValidationError("Discount cannot be negative.")

    if discount > subtotal:
        raise ValidationError(
            "Discount cannot exceed the subtotal."
        )

    # Validate additional charges.
    charge_rows = []
    charge_total = ZERO

    allowed_charge_types = {
        "packaging",
        "delivery",
        "service",
        "other",
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

        if not amount.is_finite() or amount < ZERO:
            raise ValidationError(
                "Charges must be valid non-negative amounts."
            )

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

    # ----------------------------------------
    # REWARD POINTS VALIDATION AND CALCULATION
    # ----------------------------------------

    try:
        reward_points_decimal = Decimal(
            str(reward_points_to_redeem or 0)
        )
    except (TypeError, ValueError, ArithmeticError):
        raise ValidationError(
            "Invalid reward points entered."
        )

    if (
        not reward_points_decimal.is_finite()
        or reward_points_decimal != reward_points_decimal.to_integral_value()
    ):
        raise ValidationError(
            "Reward points must be a whole number."
        )

    reward_points_to_redeem = int(reward_points_decimal)

    if reward_points_to_redeem < 0:
        raise ValidationError(
            "Reward points cannot be negative."
        )

    reward_settings = RewardSettings.get_settings()

    if reward_settings.amount_per_point <= ZERO:
        raise ValidationError(
            "Reward points earning rate must be greater than zero."
        )

    reward_account = None
    reward_discount = ZERO
    reward_points_earned = 0

    if reward_points_to_redeem > 0:
        if not reward_settings.is_enabled:
            raise ValidationError(
                "The reward system is currently disabled."
            )

        if customer is None:
            raise ValidationError(
                "Select a registered customer to redeem reward points."
            )

    # Registered customers can earn points even without redemption.
    if customer is not None and reward_settings.is_enabled:

        reward_account, created = RewardAccount.objects.get_or_create(
            customer=customer,
        )

        reward_account = (
            RewardAccount.objects
            .select_for_update()
            .get(pk=reward_account.pk)
        )

        if reward_points_to_redeem > reward_account.available_points:
            raise ValidationError(
                "Insufficient reward points. "
                f"Available points: {reward_account.available_points}."
            )

        # One point is equivalent to ₱1.
        reward_discount = money(
            Decimal(reward_points_to_redeem)
        )

        eligible_amount = money(subtotal - discount)

        if reward_discount > eligible_amount:
            raise ValidationError(
                "Reward discount cannot exceed the eligible purchase amount."
            )

        # Do not earn points on the portion paid using reward points.
        earning_amount = money(
            eligible_amount - reward_discount
        )

        if earning_amount > ZERO:
            reward_points_earned = int(
                earning_amount // reward_settings.amount_per_point
            )

    # Reward discount is separate from standard discounts.
    total_amount = money(
        subtotal
        - discount
        - reward_discount
        + charge_total
    )

    if amount_tendered < total_amount:
        raise ValidationError(
            f"Payment is insufficient. Amount due: ₱{total_amount}"
        )

    change_amount = money(
        amount_tendered - total_amount
    )

    # Generate receipt number using the local date.
    receipt_number = (
        f"POS-{today:%Y%m%d}-{uuid4().hex[:8].upper()}"
    )

    # Create the completed sale.
    sale = Sale.objects.create(
        receipt_number=receipt_number,
        cashier=cashier,
        customer=customer,
        salesperson=salesperson,
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
        reward_discount_amount=reward_discount,
        reward_points_redeemed=reward_points_to_redeem,
        reward_points_earned=reward_points_earned,
        general_note=str(general_note or "").strip()[:500],
        total_amount=total_amount,
        amount_tendered=amount_tendered,
        change_amount=change_amount,
        payment_method=payment_method,
        status=Sale.Status.COMPLETED,
    )

    # Save additional charges.
    for row in charge_rows:
        SaleCharge.objects.create(
            sale=sale,
            **row,
        )

    # Deduct allocated stock and record sale items/movements.
    for allocation in allocations:
        batch = allocation["batch"]
        quantity = allocation["quantity"]

        batch.quantity_remaining -= quantity
        batch.save(
            update_fields=["quantity_remaining"]
        )

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

    # ----------------------------------------
    # PROCESS REWARD POINTS
    # ----------------------------------------

    if reward_account is not None:

        # 1. Deduct redeemed points.
        if reward_points_to_redeem > 0:

            reward_account.available_points -= (
                reward_points_to_redeem
            )

            RewardTransaction.objects.create(
                customer=customer,
                transaction_type=(
                    RewardTransaction.TransactionType.REDEEM
                ),
                points=reward_points_to_redeem,
                balance_after=reward_account.available_points,
                pending_after=reward_account.pending_deduction,
                sale=sale,
                description=(
                    f"Redeemed {reward_points_to_redeem} points "
                    f"for receipt {receipt_number}."
                ),
            )

        # 2. Apply pending deductions to newly earned points.
        pending_applied = min(
            reward_account.pending_deduction,
            reward_points_earned,
        )

        if pending_applied > 0:

            reward_account.pending_deduction -= pending_applied

            RewardTransaction.objects.create(
                customer=customer,
                transaction_type=(
                    RewardTransaction.TransactionType.PENDING_DEDUCTION
                ),
                points=pending_applied,
                balance_after=reward_account.available_points,
                pending_after=reward_account.pending_deduction,
                sale=sale,
                description=(
                    f"Applied {pending_applied} pending point "
                    f"deductions from receipt {receipt_number}."
                ),
            )

        # 3. Credit the remaining earned points.
        credited_points = (
            reward_points_earned - pending_applied
        )

        if credited_points > 0:

            reward_account.available_points += credited_points

            RewardTransaction.objects.create(
                customer=customer,
                transaction_type=(
                    RewardTransaction.TransactionType.EARN
                ),
                points=credited_points,
                balance_after=reward_account.available_points,
                pending_after=reward_account.pending_deduction,
                sale=sale,
                description=(
                    f"Earned {credited_points} reward points "
                    f"from receipt {receipt_number}."
                ),
            )

        # 4. Save the updated account.
        reward_account.save(
            update_fields=[
                "available_points",
                "pending_deduction",
                "updated_at",
            ]
        )

    return sale
