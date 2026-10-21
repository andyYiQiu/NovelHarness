"""Event 表模型。"""
from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, Text

from ..Database import Base


class Event(Base):
    __tablename__ = "events"

    task_id = Column(Text, primary_key=True)
    node_id = Column(Text, ForeignKey("time_nodes.node_id"), nullable=False)
    event_order = Column(Integer, CheckConstraint("event_order > 0"), nullable=False)
    todo = Column(Text, nullable=False)
    epistemic_status = Column(Text)  # 旧 repository 没有独立字段时保留 NULL。
    time_text = Column(Text)
    data_json = Column(Text, CheckConstraint("json_valid(data_json)"), nullable=False)

    __table_args__ = (
        Index("events_time", "node_id", "event_order"),
    )
