
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from .models import (
    Sale,
    SaleItem,
    SaleAdjustment,
    SaleAdjustmentItem,
)
from inventory.models import StockBatch, StockMovement


CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value):
    return Decimal(str(value)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def new_adjustment_number():
    return (
        f"ADJ-{date.today():%Y%m%d}-"
        f"{uuid4().hex[:8].upper()}"
    )


@transaction.atomic
def process_adjustment(
    *,
    sale_id,
    adjustment_type,
    reason,
    processed_by,
    items=None,
    refund_amount=None,
):
    reason = (reason or "").strip()

    if not reason:
        raise ValidationError("An adjustment reason is required.")

    allowed_types = SaleAdjustment.AdjustmentType.values

    if adjustment_type not in allowed_types:
        raise ValidationError("Invalid adjustment type.")

    sale = (
        Sale.objects.select_for_update()
        .filter(pk=sale_id)
        .first()
    )

    if sale is None:
        raise ValidationError("Sale not found.")

    if sale.status != Sale.Status.COMPLETED:
        raise ValidationError(
            "Only completed sales can be adjusted."
        )

    previous_adjustments = SaleAdjustment.objects.filter(
        sale=sale,
        status=SaleAdjustment.Status.COMPLETED,
    )

    previously_adjusted = previous_adjustments.aggregate(
        total=Sum("amount")
    )["total"] or ZERO

    remaining_refundable = money(
        sale.total_amount - previously_adjusted
    )

    if remaining_refundable < ZERO:
        raise ValidationError(
            "Existing adjustments exceed the sale total."
        )

    # VOID: restore all sold quantities and void the sale.
    if adjustment_type == SaleAdjustment.AdjustmentType.VOID:
        if previous_adjustments.exists():
            raise ValidationError(
                "This sale already has an adjustment. "
                "A sale with previous adjustments cannot be voided."
            )

        sale_items = list(
            SaleItem.objects.filter(sale=sale)
            .select_related("product", "batch")
            .order_by("pk")
        )

        if not sale_items:
            raise ValidationError(
                "This sale has no items to reverse."
            )

        adjustment = SaleAdjustment.objects.create(
            adjustment_number=new_adjustment_number(),
            sale=sale,
            adjustment_type=adjustment_type,
            reason=reason,
            amount=money(sale.total_amount),
            reference_receipt_number=sale.receipt_number,
            processed_by=processed_by,
        )

        for sale_item in sale_items:
            SaleAdjustmentItem.objects.create(
                adjustment=adjustment,
                sale_item=sale_item,
                quantity=sale_item.quantity,
                amount=money(sale_item.line_total),
                restock=True,
            )

            batch = (
                StockBatch.objects.select_for_update()
                .get(pk=sale_item.batch_id)
            )

            new_quantity = (
                batch.quantity_remaining + sale_item.quantity
            )

            if new_quantity > batch.quantity_received:
                raise ValidationError(
                    f"Cannot restore stock for "
                    f"{sale_item.product.name}: "
                    "the batch quantity would exceed its "
                    "original received quantity."
                )

            batch.quantity_remaining = new_quantity
            batch.save(update_fields=["quantity_remaining"])

            StockMovement.objects.create(
                product=sale_item.product,
                batch=batch,
                movement_type=(
                    StockMovement.MovementType.SALE_RETURN
                ),
                quantity=sale_item.quantity,
                reference=adjustment.adjustment_number,
                notes=(
                    f"Stock restored for voided receipt "
                    f"{sale.receipt_number}."
                ),
                created_by=processed_by,
            )

        sale.status = Sale.Status.VOIDED
        sale.save(update_fields=["status"])

        return adjustment

    # REFUND: record a money-only refund. No stock is restored.
    if adjustment_type == SaleAdjustment.AdjustmentType.REFUND:
        try:
            amount = money(refund_amount)
        except (TypeError, ValueError, ArithmeticError):
            raise ValidationError("Enter a valid refund amount.")

        if amount <= ZERO:
            raise ValidationError(
                "Refund amount must be greater than zero."
            )

        if amount > remaining_refundable:
            raise ValidationError(
                f"Refund exceeds the remaining refundable "
                f"amount of ₱{remaining_refundable}."
            )

        return SaleAdjustment.objects.create(
            adjustment_number=new_adjustment_number(),
            sale=sale,
            adjustment_type=adjustment_type,
            reason=reason,
            amount=amount,
            reference_receipt_number=sale.receipt_number,
            processed_by=processed_by,
        )

    # RETURN: return selected items, optionally restoring stock.
    if adjustment_type == SaleAdjustment.AdjustmentType.RETURN:
        if not isinstance(items, list) or not items:
            raise ValidationError(
                "Select at least one item to return."
            )

        sold_items = {
            item.pk: item
            for item in SaleItem.objects.filter(sale=sale)
            .select_related("product", "batch")
            .order_by("pk")
        }

        requested = {}

        for entry in items:
            try:
                item_id = int(entry["sale_item_id"])
                quantity = Decimal(str(entry["quantity"]))
                restock = entry.get("restock", True)

                if not isinstance(restock, bool):
                    raise ValueError

            except (KeyError, TypeError, ValueError, ArithmeticError):
                raise ValidationError(
                    "Invalid returned item or quantity."
                )

            if item_id not in sold_items:
                raise ValidationError(
                    "A selected item does not belong to this receipt."
                )

            if quantity <= ZERO:
                raise ValidationError(
                    "Return quantity must be greater than zero."
                )

            if quantity != quantity.quantize(
                Decimal("0.001")
            ):
                raise ValidationError(
                    "Return quantity supports up to 3 decimal places."
                )

            if item_id in requested:
                old_quantity, old_restock = requested[item_id]

                if old_restock != restock:
                    raise ValidationError(
                        "Do not submit the same item with "
                        "different restock settings."
                    )

                requested[item_id] = (
                    old_quantity + quantity,
                    restock,
                )
            else:
                requested[item_id] = (quantity, restock)

        return_rows = []
        return_total = ZERO

        for item_id, (quantity, restock) in requested.items():
            sale_item = sold_items[item_id]

            already_returned = ZERO

            previous_return_items = SaleAdjustmentItem.objects.filter(
                sale_item=sale_item,
                adjustment__sale=sale,
                adjustment__adjustment_type=(
                    SaleAdjustment.AdjustmentType.RETURN
                ),
                adjustment__status=SaleAdjustment.Status.COMPLETED,
            ).aggregate(total=Sum("quantity"))

            already_returned = (
                previous_return_items["total"] or ZERO
            )

            available_quantity = (
                sale_item.quantity - already_returned
            )

            if quantity > available_quantity:
                raise ValidationError(
                    f"Cannot return {quantity} of "
                    f"{sale_item.product.name}. "
                    f"Remaining returnable quantity: "
                    f"{available_quantity}."
                )

            # Allocate the sale-level discount proportionally.
            if sale.subtotal > ZERO:
                net_factor = (
                    sale.subtotal - sale.discount
                ) / sale.subtotal
            else:
                net_factor = Decimal("1")

            unit_refund = (
                sale_item.line_total
                / sale_item.quantity
                * net_factor
            )

            item_amount = money(unit_refund * quantity)

            return_rows.append({
                "sale_item": sale_item,
                "quantity": quantity,
                "amount": item_amount,
                "restock": restock,
            })

            return_total += item_amount

        return_total = money(return_total)

        if return_total <= ZERO:
            raise ValidationError(
                "The calculated return amount must be greater than zero."
            )

        if return_total > remaining_refundable:
            raise ValidationError(
                f"Return exceeds the remaining refundable "
                f"amount of ₱{remaining_refundable}."
            )

        adjustment = SaleAdjustment.objects.create(
            adjustment_number=new_adjustment_number(),
            sale=sale,
            adjustment_type=adjustment_type,
            reason=reason,
            amount=return_total,
            reference_receipt_number=sale.receipt_number,
            processed_by=processed_by,
        )

        for row in return_rows:
            sale_item = row["sale_item"]

            SaleAdjustmentItem.objects.create(
                adjustment=adjustment,
                sale_item=sale_item,
                quantity=row["quantity"],
                amount=row["amount"],
                restock=row["restock"],
            )

            if not row["restock"]:
                continue

            batch = (
                StockBatch.objects.select_for_update()
                .get(pk=sale_item.batch_id)
            )

            new_quantity = (
                batch.quantity_remaining + row["quantity"]
            )

            if new_quantity > batch.quantity_received:
                raise ValidationError(
                    f"Cannot restore stock for "
                    f"{sale_item.product.name}: "
                    "the batch quantity would exceed its "
                    "original received quantity."
                )

            batch.quantity_remaining = new_quantity
            batch.save(update_fields=["quantity_remaining"])

            StockMovement.objects.create(
                product=sale_item.product,
                batch=batch,
                movement_type=(
                    StockMovement.MovementType.SALE_RETURN
                ),
                quantity=row["quantity"],
                reference=adjustment.adjustment_number,
                notes=(
                    f"Returned item from receipt "
                    f"{sale.receipt_number}."
                ),
                created_by=processed_by,
            )

        return adjustment
