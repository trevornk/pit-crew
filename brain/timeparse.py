"""Forgiving parsing of "when" phrases for departure times ("tomorrow 7:30 AM", "Mon 8am", ISO)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_ABBR = {d[:3]: i for i, d in enumerate(_WEEKDAYS)}
_ABBR.update({"tues": 1, "weds": 2, "thur": 3, "thurs": 3})
_TIME = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?![\d:])", re.I)


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_MONTH_DAY = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
                        r"|\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b")
_ORDINAL = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)\b")
_DOT_TIME = re.compile(r"\b(\d{1,2})\.(\d{2})\s*(am|pm|a\.m\.|p\.m\.)", re.I)


class WhenError(ValueError):
    pass


def parse_when(text: str, now: datetime) -> datetime:
    """Parse ``text`` to an aware datetime in ``now``'s timezone, strictly in the future.

    Accepts ISO-8601, ``HH:MM`` / ``7am`` / ``7:30 PM`` optionally prefixed by ``today``,
    ``tonight``, ``tomorrow`` or a weekday name. A bare time means its next occurrence.
    """
    raw = (text or "").strip()
    if not raw:
        raise WhenError("no departure time given")
    tz = now.tzinfo
    # ISO first
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        dt = dt.astimezone(tz)
        if dt <= now:
            raise WhenError(f"'{text}' is in the past")
        return dt
    except WhenError:
        raise
    except ValueError:
        pass
    s = _DOT_TIME.sub(r"\1:\2 \3", raw.lower())             # "7.30am" is 7:30 am
    explicit_date: Optional[Tuple[int, int]] = None
    md = _MONTH_DAY.search(s)
    if md:                                                    # "Nov 10 7am": the 10 is a date, not the hour
        mon, day = (md.group(1), md.group(2)) if md.group(1) else (md.group(4), md.group(3))
        explicit_date = (_MONTHS[mon], int(day))
        s = s[:md.start()] + " " + s[md.end():]
    s = _ORDINAL.sub(" ", s)                                  # "Friday the 14th at 8am": drop the ordinal
    day_offset: Optional[int] = None
    weekday: Optional[int] = None
    if "tomorrow" in s:
        day_offset = 1
        s = s.replace("tomorrow", " ")
    elif "tonight" in s or "today" in s:
        day_offset = 0
        s = s.replace("tonight", " ").replace("today", " ")
    else:
        for word in re.findall(r"[a-z]+", s):
            if word in _WEEKDAYS:
                weekday = _WEEKDAYS.index(word)
                break
            if word in _ABBR:
                weekday = _ABBR[word]
                break
    hour, minute = _find_time(s, evening=day_offset == 0 and "tonight" in raw.lower())
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if explicit_date is not None:
        try:
            target = base.replace(month=explicit_date[0], day=explicit_date[1])
        except ValueError:
            raise WhenError(f"'{text}' is not a valid date")
        if target <= now:
            try:
                target = target.replace(year=target.year + 1)
            except ValueError:    # Feb 29 and next year is not a leap year
                raise WhenError(f"'{text}' does not exist next year; give a different date")
    elif day_offset is not None:
        target = base + timedelta(days=day_offset)
    elif weekday is not None:
        ahead = (weekday - now.weekday()) % 7
        target = base + timedelta(days=ahead)
        if target <= now:
            target += timedelta(days=7)
    else:
        target = base if base > now else base + timedelta(days=1)
    if target <= now:
        raise WhenError(f"'{text}' is in the past")
    return target


def _find_time(s: str, evening: bool = False) -> Tuple[int, int]:
    found = list(_TIME.finditer(s))
    if not found:
        raise WhenError("could not find a time of day (try '7:30 AM' or 'tomorrow 07:30')")
    # prefer an unmistakable time (has ':' or am/pm) over a stray bare number
    m = next((x for x in found if x.group(2) or x.group(3)), found[0])
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    mer = (m.group(3) or "").lower().replace(".", "")
    if evening and not mer and 1 <= hour < 12:
        hour += 12                                            # "tonight 8" means 8 pm
    if mer == "pm" and hour < 12:
        hour += 12
    elif mer == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise WhenError(f"'{m.group(0)}' is not a valid time")
    if not mer and not m.group(2) and hour > 24:
        raise WhenError("ambiguous time")
    return hour, minute


def fmt_clock(dt: datetime) -> str:
    """'7:30 AM' (portable: no %-I)."""
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_day_clock(dt: datetime) -> str:
    """'Tue 7:30 AM'."""
    return f"{dt.strftime('%a')} {fmt_clock(dt)}"
