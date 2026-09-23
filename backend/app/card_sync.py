"""Resolve CDK records from the distributed store with a legacy bridge.

The original Node service keeps its card inventory in ``data/cards.json``. During
the migration period the Python API must be able to adopt a valid card from that
service into PostgreSQL, otherwise the two local entry points appear to use the
same application while accepting different CDKs.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .legacy import legacy_client
from .models import QuotaCard
from .security import card_hash, mask_owner


logger = logging.getLogger("tilian.cards")


async def resolve_card(
    session: AsyncSession,
    code: str,
    *,
    for_update: bool = False,
) -> tuple[QuotaCard | None, bool]:
    """Return a PostgreSQL card, importing a valid legacy card when necessary.

    The boolean indicates whether a row was imported during this call. Callers
    that only read a card should commit when it is true; task/fusion mutations
    already commit as part of their normal transaction.
    """

    normalized = str(code or "").strip().upper()
    if not normalized:
        return None, False

    query = select(QuotaCard).where(QuotaCard.code_hash == card_hash(normalized))
    if for_update:
        query = query.with_for_update()
    card = await session.scalar(query)
    if card or get_settings().execution_mode != "distributed":
        return card, False

    try:
        result: Any = await legacy_client.check_card(normalized)
    except HTTPException as exc:
        # A 400/404 means the legacy store does not contain this CDK. A 503 is
        # also treated as a miss so the new API remains usable if the old
        # compatibility service is stopped.
        logger.info("legacy CDK lookup failed: status=%s", exc.status_code)
        return None, False
    except Exception as exc:  # pragma: no cover - defensive network boundary
        logger.warning("legacy CDK lookup unavailable: %s", exc)
        return None, False

    if not isinstance(result, dict) or not result.get("ok"):
        return None, False
    legacy_card = result.get("card") if isinstance(result.get("card"), dict) else result
    quota = int(legacy_card.get("quota") or 0)
    if quota <= 0:
        return None, False

    # A concurrent request may have imported the same code while the legacy
    # lookup was in flight. Re-check before inserting to keep the unique hash
    # constraint from turning a valid validation into a 500 response.
    query = select(QuotaCard).where(QuotaCard.code_hash == card_hash(normalized))
    if for_update:
        query = query.with_for_update()
    card = await session.scalar(query)
    if card:
        return card, False

    card = QuotaCard(
        code_hash=card_hash(normalized),
        code_value=normalized,
        code_mask=mask_owner(normalized),
        quota=quota,
        used_quota=max(0, int(legacy_card.get("usedQuota") or 0)),
        reserved_quota=max(0, int(legacy_card.get("reservedQuota") or 0)),
        status="active",
    )
    session.add(card)
    await session.flush()
    logger.info("adopted legacy CDK into PostgreSQL: %s", card.code_mask)
    return card, True


__all__ = ["resolve_card"]
