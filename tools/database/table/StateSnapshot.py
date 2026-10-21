"""StateSnapshot 表模型。"""
from sqlalchemy import CheckConstraint, Column, ForeignKey, Text

from ..Database import Base


class StateSnapshot(Base):
    __tablename__ = "state_snapshots"

    node_id = Column(Text, ForeignKey("time_nodes.node_id"), primary_key=True)
    previous_node_id = Column(Text, ForeignKey("time_nodes.node_id"))
    data_json = Column(Text, CheckConstraint("json_valid(data_json)"), nullable=False)
