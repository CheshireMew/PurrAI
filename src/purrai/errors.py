"""Application errors shared by all administration entry points."""


class ManagementError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
