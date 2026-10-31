"""Backward-compatible calendar API (data + scheduler)."""

from calendar_data import (
    CACHE_SECONDS,
    DEFAULT_CALENDAR_TIMEZONE,
    ERROR_CACHE_SECONDS,
    GRACE_MINUTES,
    LOOKAHEAD_DAYS,
    STATE_RETENTION_DAYS,
    CalendarError,
    CalendarFeedCache,
    expand_events,
    fetch_ics,
    load_calendar_config,
    load_calendar_state,
    match_event,
    new_id,
    parse_offset,
    save_calendar_config,
    save_calendar_state,
)
from calendar_scheduler import CalendarScheduler

__all__ = [
    "CACHE_SECONDS",
    "DEFAULT_CALENDAR_TIMEZONE",
    "ERROR_CACHE_SECONDS",
    "GRACE_MINUTES",
    "LOOKAHEAD_DAYS",
    "STATE_RETENTION_DAYS",
    "CalendarError",
    "CalendarFeedCache",
    "CalendarScheduler",
    "expand_events",
    "fetch_ics",
    "load_calendar_config",
    "load_calendar_state",
    "match_event",
    "new_id",
    "parse_offset",
    "save_calendar_config",
    "save_calendar_state",
]
