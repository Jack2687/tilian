"""Read runtime/queue state without joining a consumer group or running tasks."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiokafka.admin import AIOKafkaAdminClient
from app.config import get_settings


async def main():
    settings = get_settings()
    print(json.dumps({"command_topic": settings.kafka_command_topic, "event_topic": settings.kafka_event_topic,
                      "note": "Existing tasks retain their saved outbox topic; production tasks are not migrated."}))
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap_servers.split(","), request_timeout_ms=10000)
    try:
        await admin.start()
        responses = await admin.describe_consumer_groups([settings.kafka_consumer_group])
        for response in responses:
            for group in response.groups:
                # DescribeGroups fields: error, id, state, protocol type/name, members.
                print(json.dumps({"group": group[1], "state": group[2], "error": group[0],
                                  "members": [{"id": m[0], "client": m[2] if len(m) == 6 else m[1],
                                               "host": m[3] if len(m) == 6 else m[2]}
                                              for m in group[5]]}, ensure_ascii=False))
    finally:
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
