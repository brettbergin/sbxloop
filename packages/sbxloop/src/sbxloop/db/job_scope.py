"""Read-only presentation bindings. No execution or admission metadata is changed."""

import json
from typing import Any

from sqlalchemy import select

from sbxloop.db.collaboration_models import ChannelRow
from sbxloop.db.job_models import ExternalItemRow, ExternalJobRow, ExternalRunRow


def presentation_channel_for_item(session: Any, item_id: str) -> str | None:
    row = session.get(ExternalItemRow, item_id)
    return None if row is None else str(row.channel_id)


def presentation_channel_for_run(session: Any, run_id: str) -> str | None:
    row = session.get(ExternalRunRow, run_id)
    return None if row is None else str(row.channel_id)


def channel_work_ids(session: Any, channel_id: str) -> list[str]:
    return list(
        session.scalars(
            select(ExternalJobRow.work_id).where(ExternalJobRow.channel_id == channel_id)
        )
    )


def external_metadata(session: Any, channel_id: str) -> dict[str, Any] | None:
    channel = session.get(ChannelRow, channel_id)
    if channel is not None and channel.settings_json:
        settings = json.loads(channel.settings_json)
        if isinstance(settings.get("external_work"), dict):
            return dict(settings["external_work"])
    row = session.scalars(
        select(ExternalJobRow)
        .where(ExternalJobRow.channel_id == channel_id, ExternalJobRow.system_created == 1)
        .order_by(ExternalJobRow.created_at)
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "work_id": row.work_id,
        "source": json.loads(row.source_json),
        "system_created": bool(row.system_created),
        "historical": bool(row.historical),
        "read_baseline": row.read_baseline,
        "state": row.state,
    }
