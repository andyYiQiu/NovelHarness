"""WorldData 表模型。"""
from sqlalchemy import CheckConstraint, Column, Text

from ..Database import Base


class WorldData(Base):
    __tablename__ = "world_data"

    file_path = Column(Text, primary_key=True)
    content_json = Column(Text, CheckConstraint("json_valid(content_json)"), nullable=False)
