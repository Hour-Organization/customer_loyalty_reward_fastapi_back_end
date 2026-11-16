"""Placing orders and moving them through their statuses."""

from collections import OrderedDict
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app import clock
from app.models import Address, Customer, Order, OrderItem, Payment, Product, Shipment, User
from app.serial import iso, num
from app.services import loyalty, product_stock
from app.services.records import log_activity
from app.validation import fail


def place(db: Session, customer: Customer, lines: list[dict[str, Any]], address: str, phone: str) -> Order:
    """Price the cart from the catalog, take the stock and open a cash-on-delivery order."""
    quantities: OrderedDict[int, int] = OrderedDict()
    for line in lines:
        product_id = int(line["product_id"])
        quantities[product_id] = quantities.get(product_id, 0) + int(line["quantity"])

    products = {
        product.id: product
        for product in db.scalars(
            select(Product).where(Product.id.in_(quantities)).order_by(Product.id).with_for_update()
        )
    }
    for product_id in quantities:
        product = products.get(product_id)
        if product is None or product.category is None or not product.category.is_active:
            fail("items", "An item in your cart is no longer available.")

    total = sum((Decimal(quantity) * products[pid].price for pid, quantity in quantities.items()), Decimal(0))
    delivery = db.scalars(select(Address).where(Address.customer_id == customer.id, Address.street == address)).first()
    if delivery is None:
        delivery = Address(customer_id=customer.id, street=address, city=customer.province or "Phnom Penh", country="Cambodia")
        db.add(delivery)
        db.flush()

    order = Order(
        customer_id=customer.id,
        address_id=delivery.id,
        status=Order.PENDING,
        total_amount=total,
        ordered_at=clock.now(),
        delivery_phone=phone,
    )
    db.add(order)
    db.flush()
    for product_id, quantity in quantities.items():
        product = products[product_id]
        product_stock.take(db, product, quantity, order)
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product_id,
                quantity=quantity,
                unit_price=product.price,
                base_points=product.normal_points,
                bonus_multiplier=product.bonus_multiplier,
            )
        )
    db.add(Payment(order_id=order.id, method=Payment.CASH_ON_DELIVERY, status=Payment.PENDING, amount=total))
    db.flush()
    log_activity(db, "ORDER_CREATED", f"Created order {order.reference()} for ${total:.2f}", customer)
    db.refresh(order)
    return order


def transition(db: Session, order: Order, status: str, staff: User) -> Order:
    """Move the order to its next status: shipping opens a shipment, cancelling returns the stock and
    completing pays the points."""
    db.refresh(order, with_for_update=True)
    if status not in Order.TRANSITIONS.get(order.status, []):
        fail("status", f"{order.reference()} is {order.status} and cannot become {status}.")
    order.status = status
    if status == Order.SHIPPED:
        db.add(Shipment(order_id=order.id, status=Shipment.SHIPPED, shipped_at=clock.now()))
    elif status == Order.CANCELLED:
        for item in order.items:
            product_stock.put_back(db, _product(db, item.product_id), item.quantity, order)
        if order.payment:
            order.payment.status = Payment.CANCELLED
    elif status == Order.COMPLETED:
        if order.payment:
            order.payment.status = Payment.PAID
            order.payment.paid_at = clock.now()
        db.execute(update(Shipment).where(Shipment.order_id == order.id).values(status=Shipment.DELIVERED))
        loyalty.order_completed(db, order)
    db.flush()
    log_activity(db, f"ORDER_{status}", f"Order {order.reference()} {status.lower()}", order.customer, staff)
    db.refresh(order)
    return order


def _product(db: Session, product_id: int) -> Product:
    """A product, including one deleted since it was ordered."""
    return db.scalars(select(Product).where(Product.id == product_id).execution_options(include_deleted=True)).one()


def with_relations():
    return (
        selectinload(Order.customer),
        selectinload(Order.address),
        selectinload(Order.items),
        selectinload(Order.payment),
        selectinload(Order.shipment),
    )


def to_api(db: Session, order: Order) -> dict[str, Any]:
    """The order as the API returns it."""
    ids = [item.product_id for item in order.items]
    names = dict(
        db.execute(select(Product.id, Product.name).where(Product.id.in_(ids)).execution_options(include_deleted=True)).all()
    ) if ids else {}
    payment, shipment = order.payment, order.shipment
    return {
        "id": order.id,
        "reference": order.reference(),
        "customer_id": order.customer_id,
        "customer_name": order.customer.name if order.customer else None,
        "status": order.status,
        "total": num(order.total_amount),
        "points_earned": int(order.points_earned or 0),
        "ordered_at": iso(order.ordered_at),
        "delivery_address": order.address.street if order.address else None,
        "delivery_phone": order.delivery_phone,
        "payment": {"method": payment.method, "status": payment.status} if payment else None,
        "shipment": (
            {"carrier": shipment.carrier, "tracking_no": shipment.tracking_no, "status": shipment.status} if shipment else None
        ),
        "items": [
            {
                "product_id": item.product_id,
                "name": names.get(item.product_id),
                "quantity": item.quantity,
                "unit_price": num(item.unit_price),
                "base_points": item.base_points,
                "bonus_multiplier": num(item.bonus_multiplier),
            }
            for item in order.items
        ],
    }
