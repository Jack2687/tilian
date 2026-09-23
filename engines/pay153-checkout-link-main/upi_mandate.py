"""UPI-GIT4 Checkout mandate parameters and server-side verification."""

import time


def checkout_payment_settings(now: int | None = None) -> dict:
    """Build the source worker's 30-day UPI-GIT4 mandate settings."""
    end_date = int(time.time() if now is None else now) + 30 * 24 * 60 * 60
    return {
        "payment_method_options": {
            "upi": {
                "mandate_options": {
                    "amount": 199900,
                    "amount_type": "maximum",
                    "description": "ChatGPT Plus",
                    "end_date": end_date,
                }
            }
        }
    }


def server_upi_mandate(init_data: dict) -> dict:
    options = init_data.get("payment_method_options") or {}
    upi = options.get("upi") if isinstance(options, dict) else None
    mandate = upi.get("mandate_options") if isinstance(upi, dict) else None
    return mandate if isinstance(mandate, dict) else {}


def mandate_matches(requested: dict, returned: dict) -> bool:
    if not requested or not returned:
        return False
    for key in ("amount", "end_date"):
        try:
            if int(requested[key]) != int(returned[key]):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return all(
        str(requested.get(key) or "") == str(returned.get(key) or "")
        for key in ("amount_type", "description")
    )
