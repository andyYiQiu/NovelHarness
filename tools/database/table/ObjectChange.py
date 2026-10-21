"""ObjectChange 表模型。"""
from sqlalchemy import CheckConstraint, Column, Computed, ForeignKey, ForeignKeyConstraint, Index, Text

from ..Database import Base


class ObjectChange(Base):
    __tablename__ = "object_changes"

    task_id = Column(Text, ForeignKey("events.task_id"), primary_key=True)
    object_id = Column(Text, primary_key=True)
    field = Column(Text, CheckConstraint("field IN ('owner', 'holder', 'location', 'condition')"), primary_key=True)
    ref_status = Column(Text)
    ref_id = Column(Text)
    text_value = Column(Text)
    basis = Column(Text, nullable=False)
    object_type = Column(Text, Computed("'object'", persisted=False))
    ref_type = Column(Text, Computed(
        "CASE WHEN field IN ('owner', 'holder') THEN 'role' "
        "WHEN field = 'location' THEN 'location' END", persisted=False))

    __table_args__ = (
        ForeignKeyConstraint(["object_type", "object_id"], ["assets.asset_type", "assets.asset_id"]),
        ForeignKeyConstraint(["ref_type", "ref_id"], ["assets.asset_type", "assets.asset_id"]),
        CheckConstraint(
            "(field = 'condition' AND ref_status IS NULL AND ref_id IS NULL) OR "
            "(field IN ('owner', 'holder', 'location') AND text_value IS NULL AND ref_status IS NOT NULL "
            "AND ((ref_status = 'known' AND ref_id IS NOT NULL) "
            "OR (ref_status IN ('none', 'unknown') AND ref_id IS NULL)))"),
        Index("changes_object", "object_id", "task_id"),
        Index("changes_reference", "ref_type", "ref_id", "task_id"),
    )
