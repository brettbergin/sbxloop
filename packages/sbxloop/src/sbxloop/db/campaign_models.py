"""The admitted campaign and its immutable delivery checkpoints."""

from __future__ import annotations

from sqlalchemy import REAL, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sbxloop.db.base import Base


class CampaignRow(Base):
    """A pinned scope, separate from the work-item rows discovery may replace."""

    __tablename__ = "daemon_campaigns"

    campaign_id: Mapped[str] = mapped_column(Text, primary_key=True)
    plan_hash: Mapped[str] = mapped_column(Text, nullable=False)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    held: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    prepared: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    hold_reason: Mapped[str | None] = mapped_column(Text)
    hold_actor: Mapped[str | None] = mapped_column(Text)
    blocker_item_id: Mapped[str | None] = mapped_column(Text)
    blocker_reason: Mapped[str | None] = mapped_column(Text)
    order_actor: Mapped[str | None] = mapped_column(Text)
    order_changed_at: Mapped[float | None] = mapped_column(REAL)


class CampaignStepRow(Base):
    """Stable membership and success evidence, never inferred from issue closure.

    Membership stays reserved after completion. Releasing or transferring
    it needs an explicit future scope-edit operation; rediscovery must not
    silently authorize a completed step to run again.
    """

    __tablename__ = "daemon_campaign_steps"
    __table_args__ = (
        UniqueConstraint("campaign_id", "position"),
        UniqueConstraint("source_key", "repo"),
    )

    member_key: Mapped[str] = mapped_column(Text, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        Text, ForeignKey("daemon_campaigns.campaign_id"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    step_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[float | None] = mapped_column(REAL)
