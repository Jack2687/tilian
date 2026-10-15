from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RuntimeConfig
from ..schemas import ConcurrencyConfig


async def load_concurrency_config(session: AsyncSession) -> dict[str, int]:
    """Read the shared policy for every request, without caching user snapshots."""
    config = await session.get(RuntimeConfig, "concurrency")
    values = config.value if config and isinstance(config.value, dict) else {}
    return ConcurrencyConfig(**values).model_dump()


def effective_concurrency(config: dict[str, int]) -> int:
    """Ordinary users share one per-task limit; VIP tiers are not implemented.

    globalLimit is retained for API compatibility and caps each new task.
    It is not a distributed semaphore across multiple workers/tasks.
    """
    return max(1, min(config["default"], config["globalLimit"], 100))
