"""EventObject 表模型。"""
from sqlalchemy import CheckConstraint, Column, Computed, ForeignKey, ForeignKeyConstraint, Index, Integer, Text

from ..Database import Base


class EventObject(Base):
    __tablename__ = "event_objects"

    task_id = Column(Text, ForeignKey("events.task_id"), primary_key=True)
    position = Column(Integer, CheckConstraint("position > 0"), primary_key=True)
    object_id = Column(Text, nullable=False)
    object_type = Column(Text, Computed("'object'", persisted=False))

    __table_args__ = (
        ForeignKeyConstraint(["object_type", "object_id"], ["assets.asset_type", "assets.asset_id"]),
        Index("events_object", "object_id", "task_id"),
    )
