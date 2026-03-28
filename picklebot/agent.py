"""Agent layer: interpret user commands into structured instructions.

This module sends the user command to OpenAI and parses the response into a
simple JSON structure that the browser automation layer can act upon.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, Optional

import dateparser
import openai
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv

from . import prompts


load_dotenv()


def _get_api_key() -> Optional[str]:
    """Return the OpenAI API key from environment, if available."""
    return os.getenv("OPENAI_API_KEY")


def _normalize_date(value: Optional[str]) -> Optional[str]:
    """Normalize a date-like string into ISO date (YYYY-MM-DD)."""
    if not value or not isinstance(value, str):
        return None

    text = value.strip().lower()

    # Handle day-of-week names explicitly
    days_of_week = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    
    # Strip out common prefixes like "this", "next", "last" to find the day name
    prefixes_to_strip = ["this ", "next ", "last ", "upcoming "]
    clean_text = text
    for prefix in prefixes_to_strip:
        if clean_text.startswith(prefix):
            clean_text = clean_text[len(prefix):]
            break
    
    # Check if it's a day of the week
    if clean_text in days_of_week:
        today = datetime.datetime.now()
        target_day = days_of_week[clean_text]
        days_ahead = target_day - today.weekday()
        
        # If we're looking for "this" day and it hasn't happened yet this week, use it
        # If it's past or we have no prefix info, get the next occurrence
        if days_ahead < 0 or (days_ahead == 0 and "this" not in text):
            days_ahead += 7
        elif days_ahead == 0 and "this" in text:
            # "this monday" when today is monday = today
            days_ahead = 0
        
        result_date = today + datetime.timedelta(days=days_ahead)
        return result_date.date().isoformat()
    
    # Try dateparser for other formats (tomorrow, today, specific dates, etc.)
    parsed = dateparser.parse(value, settings={"PREFER_DATES_FROM": "future"})
    if not parsed:
        return value.strip()

    return parsed.date().isoformat()


def _normalize_time(value: Optional[str]) -> Optional[str]:
    """Normalize a time string (e.g. "8pm" → "20:00")."""
    if not value or not isinstance(value, str):
        return None

    import re
    # If it's a bare integer (e.g. "8" or "10"), append ":00" so dateutil
    # treats it as a time (8:00) rather than a date (August).
    bare_hour = re.match(r"^(\d{1,2})(am|pm)?$", value.strip(), re.IGNORECASE)
    if bare_hour:
        value = bare_hour.group(1) + ":00" + (bare_hour.group(2) or "")

    try:
        # Use a fixed date so only the time portion matters.
        dt = dateutil_parser.parse(value, default=datetime.datetime(2000, 1, 1))
        return dt.time().strftime("%H:%M")
    except Exception:
        return value.strip()


def _normalize_time_range(value: Optional[str]) -> Optional[str]:
    """Normalize a time-range string (e.g. "7pm-9pm")."""
    if not value or not isinstance(value, str):
        return None

    parts = [p.strip() for p in value.replace("to", "-").split("-") if p.strip()]
    if len(parts) != 2:
        return value.strip()

    start = _normalize_time(parts[0])
    end = _normalize_time(parts[1])
    if start and end:
        return f"{start}-{end}"

    return value.strip()


def _validate_instructions(instr: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the parsed instructions.

    This adds a `valid` boolean and `validation_error` string if invalid.
    """
    instr = dict(instr)  # work on a copy

    instr["action"] = (instr.get("action") or "").strip().lower()
    instr["date"] = _normalize_date(instr.get("date"))
    instr["time"] = _normalize_time(instr.get("time"))
    instr["time_range"] = _normalize_time_range(instr.get("time_range"))

    instr.setdefault("court", None)
    instr.setdefault("raw_command", "")

    # Basic schema validation.
    if instr["action"] not in {"check_availability", "book"}:
        instr["valid"] = False
        instr["validation_error"] = (
            f"Unrecognized action '{instr.get('action')}'. "
            "Try phrases like 'check availability' or 'book'."
        )
        return instr

    if instr["action"] == "check_availability":
        # Check availability requires a date
        if not instr["date"]:
            instr["valid"] = False
            instr["validation_error"] = "Could not determine a date from that command."
            return instr

    if instr["action"] == "book":
        if not instr["court"]:
            instr["valid"] = False
            instr["validation_error"] = (
                "Booking requires a court (e.g., '1A', '2B')."
            )
            return instr

        if not (instr["time"] or instr["time_range"]):
            instr["valid"] = False
            instr["validation_error"] = (
                "Booking requires a time (e.g., '8pm') or a time range (e.g., '7pm-9pm')."
            )
            return instr
        
        # For booking, date is optional - main.py can provide context-aware date
        # if the user is already on the availability page

    instr["valid"] = True
    return instr


def _extract_time_from_raw(command: str) -> Optional[str]:
    """Best-effort extraction of a time string directly from the raw command.

    Used as a fallback when the model omits time from its extracted fields.
    Looks for patterns like '6:30', '8pm', '8:30pm', '20:00'.
    """
    import re
    # Match patterns like 6:30, 8pm, 8:30pm, 8:30 PM, 20:00
    pattern = re.compile(
        r"\b(\d{1,2}(?::\d{2})?(?:\s?[ap]m)?)\b",
        re.IGNORECASE,
    )
    match = pattern.search(command)
    if match:
        return _normalize_time(match.group(1))
    return None


def revalidate_instructions(instr: Dict[str, Any]) -> Dict[str, Any]:
    """Re-validate a merged instruction dict, e.g. after combining pending context."""
    return _validate_instructions(dict(instr))


def interpret_command_with_context(command: str, existing: Dict[str, Any]) -> Dict[str, Any]:
    """Interpret a follow-up command that builds on already-known fields.

    Synthesizes a combined command string so the model has full context
    before parsing (e.g., already knows action=book, now user gives the date).
    """
    parts = []
    action = existing.get("action")
    if action == "check_availability":
        parts.append("check availability")
    elif action == "book":
        parts.append("book")

    court = existing.get("court")
    if court:
        parts.append(f"court {court}")

    date = existing.get("date")
    if date:
        parts.append(f"on {date}")

    time = existing.get("time")
    if time:
        parts.append(f"at {time}")
    elif existing.get("time_range"):
        parts.append(f"between {existing['time_range']}")

    combined = " ".join(parts) + (", " + command if parts else command)
    return interpret_command(combined)


def interpret_command(command: str) -> Dict[str, Any]:
    """Convert a natural language command into structured instructions.

    The returned dict is expected to include at least these keys:
      - action: "check_availability" or "book"
      - date: natural date string ("today", "tomorrow", etc.)
      - time: time string ("8pm") or None
      - raw_command: the original user input
    """

    api_key = _get_api_key()
    if not api_key:
        return {
            "action": "check_availability",
            "date": "today",
            "time": None,
            "raw_command": command,
            "error": "OPENAI_API_KEY is not set. Please add it to your .env file.",
        }

    # Create a client instance to avoid module-level initialization issues.
    client = openai.OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompts.COMMAND_TO_JSON},
                {"role": "user", "content": command},
            ],
            temperature=0.0,
            max_tokens=250,
        )

        content = response.choices[0].message.content.strip()
        # Attempt to parse JSON from the model response.
        parsed = json.loads(content)

        if isinstance(parsed, dict):
            # If the model returned a partial response (missing mandatory fields),
            # promote the extracted fields to top-level so main.py context
            # (e.g. current availability date) can fill in what's missing.
            if "extracted" in parsed:
                promoted = dict(parsed["extracted"])
                promoted["raw_command"] = command
                promoted.setdefault("court", None)
                promoted.setdefault("time_range", None)
                # Safety net: if the model dropped time from extracted, try to
                # recover it directly from the raw command using dateutil.
                if not promoted.get("time"):
                    promoted["time"] = _extract_time_from_raw(command)
                else:
                    promoted.setdefault("time", None)
                validated = _validate_instructions(promoted)
                if not validated.get("valid"):
                    validated["needs_more_info"] = True
                    validated["_feedback"] = parsed.get("feedback", "I need a bit more information.")
                    validated["_partial"] = {k: promoted.get(k) for k in ("action", "date", "court", "time", "time_range")}
                return validated

            parsed.setdefault("court", None)
            parsed.setdefault("time", None)
            parsed.setdefault("time_range", None)
            parsed["raw_command"] = command
            return _validate_instructions(parsed)

        raise ValueError("Response did not contain a JSON object.")

    except Exception as exc:
        # Fallback: return a stub and surface the error.
        fallback = {
            "action": "check_availability",
            "date": "today",
            "time": None,
            "court": None,
            "raw_command": command,
            "error": str(exc),
        }
        return _validate_instructions(fallback)
