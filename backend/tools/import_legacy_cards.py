from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal, create_schema
from app.models import QuotaCard
from app.security import card_hash, mask_owner


async def migrate(path: Path) -> None:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("cards file must contain a JSON array")
    await create_schema()
    async with SessionLocal() as session:
        imported = 0
        for record in records:
            code = str(record.get("code") or "").strip().upper()
            if not code:
                continue
            hashed = card_hash(code)
            card = await session.scalar(select(QuotaCard).where(QuotaCard.code_hash == hashed))
            if not card:
                card = QuotaCard(code_hash=hashed, code_value=code, code_mask=mask_owner(code), quota=int(record.get("quota") or 0))
                session.add(card)
            card.code_value = code
            card.code_mask = mask_owner(code)
            card.quota = int(record.get("quota") or 0)
            card.used_quota = int(record.get("usedQuota") or 0)
            card.reserved_quota = 0
            card.status = "disabled" if record.get("status") == "disabled" else "active"
            imported += 1
        await session.commit()
    print(f"imported {imported} cards")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    asyncio.run(migrate(args.path))
