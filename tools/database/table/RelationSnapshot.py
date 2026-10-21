"""RelationSnapshot 表模型。"""
from sqlalchemy import CheckConstraint, Column, Computed, ForeignKey, ForeignKeyConstraint, Index, Text

from ..Database import Base


class RelationSnapshot(Base):
    __tablename__ = "relation_snapshots"

    node_id = Column(Text, ForeignKey("state_snapshots.node_id"), primary_key=True)
    owner_id = Column(Text, primary_key=True)
    other_id = Column(Text, CheckConstraint("other_id <> owner_id"), primary_key=True)
    relation = Column(Text, primary_key=True)
    status = Column(Text, CheckConstraint("status IN ('active', 'inactive', 'unknown')"), nullable=False)
    detail = Column(Text)
    role_type = Column(Text, Computed("'role'", persisted=False))

    __table_args__ = (
        ForeignKeyConstraint(["role_type", "owner_id"], ["assets.asset_type", "assets.asset_id"]),
        ForeignKeyConstraint(["role_type", "other_id"], ["assets.asset_type", "assets.asset_id"]),
        Index("relations_owner", "owner_id", "other_id", "node_id"),
        Index("relations_other", "other_id", "node_id"),
    )
