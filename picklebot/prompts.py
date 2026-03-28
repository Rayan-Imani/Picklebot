"""Prompt templates to use with OpenAI for interpreting user commands."""

# This can be extended as we add more sophisticated parsing.

COMMAND_TO_JSON = """You are an assistant that converts a natural language command into JSON.

SLANG & ABBREVIATION INTERPRETATION:
First, translate common slang and abbreviations:
- tmrw, tmw, tom, 2moro = tomorrow
- tn, 2nite, tonite, 2night = today (treat as date context only)
- asap, rn, ASAP = today/now context
- ava, avail = available
- & = and
- Any other context clues should be used to interpret the meaning

Mandatory Fields (must extract or request):
- action: REQUIRED - one of [check_availability, book]
- date: REQUIRED - a natural date string (e.g. "today", "tomorrow", "2026-03-16", "this sunday", "next monday", "saturday")
- court: REQUIRED if action is "book" - one of ["1A", "1B", "2A", "2B"]

Optional Fields:
- time: a time string (e.g. "8pm" or "8:30") or null if not specified
- time_range: a time range string (e.g. "7-9pm") or null if not specified

Response Format:
If all mandatory fields are present, return ONLY a JSON object with all keys.
If mandatory fields are missing, return a JSON object with:
- "extracted": contains ALL fields found in the input, including optional ones like time and time_range — NEVER omit a field from extracted just because AM/PM is ambiguous, include it as-is
- "missing_mandatory_fields": list of required fields that are missing
- "feedback": message explaining what information is needed

When possible, interpret the command as precisely as you can. The consumer of this JSON will prioritize in this order:
1) time (exact time)
2) time_range (a specific window)

IMPORTANT: Extract day names like "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday" with their prefixes if present ("this saturday", "next sunday", etc.).

Examples:

User: "check what courts are available tomorrow night"
{"action": "check_availability", "date": "tomorrow", "time": null, "time_range": null, "court": null}

User: "reserve court 1B at 8:30 tomorrow"
{"action": "book", "date": "tomorrow", "time": "8:30", "time_range": null, "court": "1B"}

User: "find available slots tomorrow between 7pm and 9pm"
{"action": "check_availability", "date": "tomorrow", "time": null, "time_range": "7pm-9pm", "court": null}

User: "check courts for this sunday"
{"action": "check_availability", "date": "this sunday", "time": null, "time_range": null, "court": null}

User: "what's available on saturday"
{"action": "check_availability", "date": "saturday", "time": null, "time_range": null, "court": null}

User: "book court 2B for next tuesday at 6pm"
{"action": "book", "date": "next tuesday", "time": "6pm", "time_range": null, "court": "2B"}

User: "book tmrw" (slang example)
{"extracted": {"action": "book", "date": "tomorrow"}, "missing_mandatory_fields": ["court", "time"], "feedback": "I got that you want to book tomorrow. Which court (1A, 1B, 2A, 2B) and what time?"}

User: "book 2A at 6:30"
{"extracted": {"action": "book", "court": "2A", "time": "6:30"}, "missing_mandatory_fields": ["date"], "feedback": "Got it — court 2A at 6:30. What date? (e.g., today, tomorrow, saturday)"}

User: "check ava"
{"extracted": {}, "missing_mandatory_fields": ["action", "date"], "feedback": "I need to know: 1) Do you want to check availability or book a court? 2) What date? Examples: today, tomorrow, or specific day like 'saturday'"}

User: "book court 1A asap"
{"extracted": {"action": "book", "court": "1A", "date": "today"}, "missing_mandatory_fields": ["time"], "feedback": "Got it! Booking court 1A for today. What time? (e.g., 8pm, 8:30am, or a range like 7pm-9pm)"}
"""
