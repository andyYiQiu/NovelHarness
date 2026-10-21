"""RelationChange 表模型。"""
from sqlalchemy import CheckConstraint, Column, Computed, ForeignKey, ForeignKeyConstraint, Index, Text

from ..Database import Base


class RelationChange(Base):
    """一行对应一个变化字段；缺行表示不变，value 为 NULL 表示明确更新为空。"""
    __tablename__ = "relation_changes"

    task_id = Column(Text, ForeignKey("events.task_id"), primary_key=True)
    owner_id = Column(Text, primary_key=True)
    other_id = Column(Text, CheckConstraint("other_id <> owner_id"), primary_key=True)
    relation = Column(Text, primary_key=True)
    field = Column(Text, CheckConstraint("field IN ('status', 'detail')"), primary_key=True)
    value = Column(Text)
    basis = Column(Text, nullable=False)
    role_type = Column(Text, Computed("'role'", persisted=False))

    __table_args__ = (
        ForeignKeyConstraint(["role_type", "owner_id"], ["assets.asset_type", "assets.asset_id"]),
        ForeignKeyConstraint(["role_type", "other_id"], ["assets.asset_type", "assets.asset_id"]),
        CheckConstraint("field = 'detail' OR (value IS NOT NULL AND value IN ('active', 'inactive', 'unknown'))"),
        Index("changes_relation_owner", "owner_id", "other_id", "task_id"),
        Index("changes_relation_other", "other_id", "task_id"),
    )
