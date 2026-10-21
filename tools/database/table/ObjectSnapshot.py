"""ObjectSnapshot 表模型。"""
from sqlalchemy import CheckConstraint, Column, Computed, ForeignKey, ForeignKeyConstraint, Index, Text

from ..Database import Base


class ObjectSnapshot(Base):
    __tablename__ = "object_snapshots"

    node_id = Column(Text, ForeignKey("state_snapshots.node_id"), primary_key=True)
    object_id = Column(Text, primary_key=True)
    owner_status = Column(Text, nullable=False)
    owner_id = Column(Text)
    holder_status = Column(Text, nullable=False)
    holder_id = Column(Text)
    location_status = Column(Text, nullable=False)
    location_id = Column(Text)
    condition = Column(Text)
    object_type = Column(Text, Computed("'object'", persisted=False))
    role_type = Column(Text, Computed("'role'", persisted=False))
    location_type = Column(Text, Computed("'location'", persisted=False))

    __table_args__ = (
        ForeignKeyConstraint(["object_type", "object_id"], ["assets.asset_type", "assets.asset_id"]),
        ForeignKeyConstraint(["role_type", "owner_id"], ["assets.asset_type", "assets.asset_id"]),
        ForeignKeyConstraint(["role_type", "holder_id"], ["assets.asset_type", "assets.asset_id"]),
        ForeignKeyConstraint(["location_type", "location_id"], ["assets.asset_type", "assets.asset_id"]),
        CheckConstraint("(owner_status = 'known' AND owner_id IS NOT NULL) "
                        "OR (owner_status IN ('none', 'unknown') AND owner_id IS NULL)"),
        CheckConstraint("(holder_status = 'known' AND holder_id IS NOT NULL) "
                        "OR (holder_status IN ('none', 'unknown') AND holder_id IS NULL)"),
        CheckConstraint("(location_status = 'known' AND location_id IS NOT NULL) "
                        "OR (location_status IN ('none', 'unknown') AND location_id IS NULL)"),
        Index("snapshots_object", "object_id", "node_id"),
        Index("snapshots_holder", "node_id", "holder_id"),
        Index("snapshots_location", "node_id", "location_id"),
    )
