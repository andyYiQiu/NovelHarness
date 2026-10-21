"""RepositoryInfo 表模型。"""
from sqlalchemy import CheckConstraint, Column, Text

from ..Database import Base


class RepositoryInfo(Base):
    __tablename__ = "repository_info"

    source = Column(Text, nullable=False)
    time_basis = Column(Text, CheckConstraint("time_basis = 'story'"), nullable=False)

    # 一份库仅导入一条来源记录；只声明 ORM 身份，不改变既有物理表结构。
    __mapper_args__ = {"primary_key": [source]}
