class EvalplaneError(Exception):
    """Base error."""


class ConfigError(EvalplaneError):
    """Invalid or missing configuration (CLI exit code 2)."""
