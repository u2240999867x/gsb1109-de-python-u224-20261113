"""Original validation entry point for order records (the "退件" gate).

Built on the local `schema` library. Cross-field rules raise SchemaError
with stable, data-independent messages so the failing rule is identifiable
without leaking record values into error output.
"""

from schema import And, Or, Regex, Schema, SchemaError

EMAIL_RE = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
TAX_ID_RE = r"^[A-Z0-9]{10,18}$"
ORDER_ID_RE = r"^ORD-\d{6}$"
SKU_RE = r"^[A-Z]{2}\d{4}$"


def check_email_confirmation(order):
    """R1 cross-field equality: customer.email must equal confirm_email."""
    if order["customer"]["email"] != order["confirm_email"]:
        raise SchemaError(
            [], ["R1 cross-field equality: customer.email != confirm_email"]
        )
    return True


def check_quantity_subtotal(order):
    """R2 quantity subtotal: sum(items[].quantity) must equal summary.total_quantity."""
    subtotal = sum(item["quantity"] for item in order["items"])
    if subtotal != order["summary"]["total_quantity"]:
        raise SchemaError(
            [], ["R2 quantity subtotal: sum(items[].quantity) != summary.total_quantity"]
        )
    return True


ORDER_SCHEMA = Schema(
    And(
        {
            "order_id": Regex(ORDER_ID_RE),
            "customer": {
                "name": And(str, len),
                "email": Regex(EMAIL_RE),
                "tax_id": Regex(TAX_ID_RE),
            },
            "confirm_email": Regex(EMAIL_RE),
            "currency": Or("USD", "EUR", "CNY"),
            "items": [
                {
                    "sku": Regex(SKU_RE),
                    "quantity": And(int, lambda q: q > 0),
                    "unit_price": And(Or(int, float), lambda p: p >= 0),
                }
            ],
            "summary": {
                "total_quantity": And(int, lambda q: q > 0),
                "total_amount": And(Or(int, float), lambda a: a >= 0),
            },
        },
        check_email_confirmation,
        check_quantity_subtotal,
    )
)


def validate_order(record):
    """The original validation entry point. Raises SchemaError on rejection."""
    return ORDER_SCHEMA.validate(record)
