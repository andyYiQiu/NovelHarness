"""TimeNode 表模型。"""
from sqlalchemy import CheckConstraint, Column, Integer, Text

from ..Database import Base


class TimeNode(Base):
    __tablename__ = "time_nodes"

    node_id = Column(Text, primary_key=True)
    node_order = Column(Integer, CheckConstraint("node_order > 0"), nullable=False, unique=True)
    node_name = Column(Text, nullable=False)
    time_relative = Column(Text)
    data_json = Column(Text, CheckConstraint("json_valid(data_json)"), nullable=False)
