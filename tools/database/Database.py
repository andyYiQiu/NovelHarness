"""数据库基础设施：模型基类和导入用引擎；不在模块导入时打开连接。"""
from sqlalchemy import URL, create_engine, event as engine_event
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_import_engine(database):
    """为此工具启用 SQLite 外键及显式事务，使建表也参与失败回滚。"""
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(database)))

    @engine_event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, _):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()

    @engine_event.listens_for(engine, "begin")
    def begin_transaction(connection):
        connection.exec_driver_sql("BEGIN")

    return engine
