"""Asset 表模型。"""
from sqlalchemy import CheckConstraint, Column, Index, Text

from ..Database import Base


class Asset(Base):
    __tablename__ = "assets"

    asset_type = Column(Text, CheckConstraint("asset_type IN ('role', 'location', 'object')"), primary_key=True)
    asset_id = Column(Text, primary_key=True)
    name = Column(Text, nullable=False)
    data_json = Column(Text, CheckConstraint("json_valid(data_json)"), nullable=False)

    __table_args__ = (
        Index("assets_name", "asset_type", "name"),
    )
