"""EventParticipant 表模型。"""
from sqlalchemy import CheckConstraint, Column, Computed, ForeignKey, ForeignKeyConstraint, Index, Integer, Text

from ..Database import Base


class EventParticipant(Base):
    """数组位置保留重复参与者；生成类型列保证引用的是人物。"""
    __tablename__ = "event_participants"

    task_id = Column(Text, ForeignKey("events.task_id"), primary_key=True)
    position = Column(Integer, CheckConstraint("position > 0"), primary_key=True)
    role_id = Column(Text, nullable=False)
    original_goal = Column(Text)
    role_type = Column(Text, Computed("'role'", persisted=False))

    __table_args__ = (
        ForeignKeyConstraint(["role_type", "role_id"], ["assets.asset_type", "assets.asset_id"]),
        Index("participants_role", "role_id", "task_id"),
    )
