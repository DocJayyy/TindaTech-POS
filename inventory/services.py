from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from inventory.models import StockBatch, StockMovement
from django.utils import timezone


@transaction.atomic
def receive_stock(
    *,
    product,
    quantity,
    unit_cost,
    supplier=None,
    batch_code="",
    expiration_date=None,
    user=None,
):
    quantity = Decimal(str(quantity))
    unit_cost = Decimal(str(unit_cost))

    if quantity <= 0:
        raise ValidationError("Quantity must be greater than zero.")

    if unit_cost < 0:
        raise ValidationError("Unit cost cannot be negative.")

    if product.tracks_expiration and not expiration_date:
        raise ValidationError(
            "An expiration date is required for this product."
        )

    batch = StockBatch.objects.create(
        product=product,
        supplier=supplier,
        batch_code=batch_code,
        quantity_received=quantity,
        quantity_remaining=quantity,
        unit_cost=unit_cost,
        expiration_date=expiration_date,
    )

    StockMovement.objects.create(
        product=product,
        batch=batch,
        movement_type=StockMovement.MovementType.STOCK_IN,
        quantity=quantity,
        reference=f"RECEIVE-{batch.pk}",
        notes="Stock received from supplier.",
        created_by=user,
    )

    return batch


@transaction.atomic
def record_damaged_stock(
    *,
    batch_id,
    quantity,
    reason,
    user=None,
):
    quantity = Decimal(str(quantity))
    reason = (reason or "").strip()

    if quantity <= 0:
        raise ValidationError(
            "Damaged quantity must be greater than zero."
        )

    if not reason:
        raise ValidationError(
            "Please provide a reason for the damaged stock."
        )

    try:
        batch = (
            StockBatch.objects
            .select_for_update()
            .select_related("product")
            .get(pk=batch_id)
        )
    except StockBatch.DoesNotExist:
        raise ValidationError("Stock batch was not found.")

    if quantity > batch.quantity_remaining:
        raise ValidationError(
            "Damaged quantity cannot exceed the remaining batch stock."
        )

    batch.quantity_remaining -= quantity
    batch.save(update_fields=["quantity_remaining"])

    movement = StockMovement.objects.create(
        product=batch.product,
        batch=batch,
        movement_type=StockMovement.MovementType.DAMAGED,
        quantity=quantity,
        reference=f"DAMAGE-{batch.pk}",
        notes=reason,
        created_by=user,
    )

    return movement


from datetime import date

# Add this import at the top of services.py:
# from django.utils import timezone


@transaction.atomic
def record_expired_stock(
    *,
    batch_id,
    quantity,
    reason,
    user=None,
):
    quantity = Decimal(str(quantity))
    reason = (reason or "").strip()

    if quantity <= 0:
        raise ValidationError(
            "Expired quantity must be greater than zero."
        )

    if not reason:
        raise ValidationError(
            "Please provide a reason for the expired stock."
        )

    try:
        batch = (
            StockBatch.objects
            .select_for_update()
            .select_related("product")
            .get(pk=batch_id)
        )
    except StockBatch.DoesNotExist:
        raise ValidationError("Stock batch was not found.")

    today = timezone.localdate()

    if not batch.expiration_date:
        raise ValidationError(
            "This batch has no recorded expiration date."
        )

    if batch.expiration_date >= today:
        raise ValidationError(
            "Only batches past their expiration date can be adjusted."
        )

    if quantity > batch.quantity_remaining:
        raise ValidationError(
            "Expired quantity cannot exceed the remaining batch stock."
        )

    batch.quantity_remaining -= quantity
    batch.save(update_fields=["quantity_remaining"])

    movement = StockMovement.objects.create(
        product=batch.product,
        batch=batch,
        movement_type=StockMovement.MovementType.EXPIRED,
        quantity=quantity,
        reference=f"EXPIRED-{batch.pk}",
        notes=reason,
        created_by=user,
    )

    return movement
