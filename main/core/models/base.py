from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 命名约定：让外键/唯一/主键等约束有稳定名字。
# 必要性：Alembic batch mode 改 SQLite 外键时，drop_constraint 需按名定位；
# SQLite 外键默认无名会报 "Constraint must have a name"。统一命名后可正确引用。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
