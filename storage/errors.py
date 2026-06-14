class DatabaseError(Exception):
    """Base error for local JSON database operations."""


class DuplicateRecordError(DatabaseError):
    """Raised when creating a record with an id that already exists."""


class RecordNotFoundError(DatabaseError):
    """Raised when updating or replacing a record that does not exist."""


class DatabaseCorruptError(DatabaseError):
    """Raised when the database file cannot be decoded."""
