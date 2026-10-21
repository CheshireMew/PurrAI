"""Small, dependency-free projections of persisted records."""


def row_dict(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}
