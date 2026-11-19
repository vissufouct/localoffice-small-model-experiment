class HarnessError(Exception):
    def __init__(self, code: str, message: str, *, recovery: dict | None = None):
        self.code = code
        self.recovery = recovery
        super().__init__(message)
