"""Exceptions raised by weekend-saver."""


class WeekendSaverError(Exception):
    """Base class for all expected weekend-saver failures."""


class ConfigError(WeekendSaverError):
    """The user-supplied resource list is missing, malformed, or unsupported."""


class ApprovalUnavailableError(WeekendSaverError):
    """A plan needs a human decision but nobody can be asked right now."""


class StateStoreError(WeekendSaverError):
    """The prior-state snapshot could not be read or written."""
