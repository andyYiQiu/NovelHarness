"""事件语义向量与稳定事件编号的显式映射；每库使用一套编码配置。"""
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, LargeBinary, Text

from ..Database import Base


class EventEmbedding(Base):
    __tablename__ = "event_embeddings"

    vector_id = Column(Integer, primary_key=True)
    task_id = Column(Text, ForeignKey("events.task_id", ondelete="CASCADE"), nullable=False, unique=True)
    embedding_model = Column(Text, nullable=False)
    model_revision = Column(Text, nullable=False)
    embedding_task = Column(Text, nullable=False)
    dimension = Column(Integer, nullable=False)
    text_hash = Column(Text, nullable=False)
    vector = Column(LargeBinary, nullable=False)

    __table_args__ = (
        CheckConstraint("vector_id > 0 AND dimension > 0"),
        CheckConstraint("length(vector) = dimension * 4"),
    )
