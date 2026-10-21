"""EventLocation 表模型。"""
from sqlalchemy import CheckConstraint, Column, Computed, ForeignKey, ForeignKeyConstraint, Index, Integer, Text

from ..Database import Base


class EventLocation(Base):
    __tablename__ = "event_locations"

    task_id = Column(Text, ForeignKey("events.task_id"), primary_key=True)
    position = Column(Integer, CheckConstraint("position > 0"), primary_key=True)
    location_id = Column(Text, nullable=False)
    location_type = Column(Text, Computed("'location'", persisted=False))

    __table_args__ = (
        ForeignKeyConstraint(["location_type", "location_id"], ["assets.asset_type", "assets.asset_id"]),
        Index("events_location", "location_id", "task_id"),
    )
