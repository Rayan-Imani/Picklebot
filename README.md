# Picklebot 🥒

An AI-powered pickleball court reservation assistant. Talk to it in plain English — it figures out what you want, navigates the reservation site, and books courts for you. Available as a Discord bot or a local CLI.

---

## Features

- **Natural language commands** — "check availability saturday", "book 2A at 6:30 tomorrow"
- **Slang support** — understands tmrw, tn, asap, ava, and more
- **Multi-turn conversation** — if a command is missing fields (e.g. no date), the bot asks for just what it needs and merges the follow-up
- **Checks all 4 courts** — 1A, 2A, 1B, 2B in a single availability check
- **Evening tab** — automatically switches to evening slots on the reservation site
- **Cross-month navigation** — navigates forward through months to reach any future date
- **Discord bot** — slash command `/ask` with a persistent browser session
- **CLI mode** — interactive terminal for local testing

---

## How It Works

1. You type a command (Discord `/ask` or CLI input)
2. **OpenAI GPT-4o-mini** parses it into structured JSON (`action`, `date`, `court`, `time`)
3. **Playwright** automates the reservation website:
   - Logs in, selects the location, picks the court
   - Navigates to the correct month and date on the calendar
   - Clicks the Evening tab, reads available time slots
   - For bookings: selects the time, clicks through to confirm

---

## Project Structure

```
picklebot/
  agent.py      # OpenAI command parsing, date/time normalization
  browser.py    # Playwright automation (login, calendar, booking)
  prompts.py    # GPT-4o-mini system prompt
discord_bot.py  # Discord slash command bot
main.py         # Local CLI entry point
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in your values:

```env
DISCORD_TOKEN=        # From discord.com/developers/applications
DISCORD_GUILD_ID=     # Your server's ID (for instant slash command sync)
OPENAI_API_KEY=       # From platform.openai.com/api-keys
COURT_SITE_URL=https://www.shawneetrail.dserec.com/
COURT_LOGIN_URL=https://www.shawneetrail.dserec.com/
COURT_USERNAME=       # Your reservation site username
COURT_PASSWORD=       # Your reservation site password
```

### 3. Run

**Discord bot:**
```bash
python discord_bot.py
```

**CLI (local testing):**
```bash
python main.py
```

---

## Usage Examples

| Command | What it does |
|---|---|
| `check availability tomorrow` | Shows all open evening slots across all 4 courts |
| `what's available saturday` | Same, for Saturday |
| `book 2A at 6:30` | Books court 2A at 6:30 PM |
| `book 1B tmrw at 8pm` | Books court 1B tomorrow at 8 PM |
| `ava tn` | Checks availability tonight |

If a field is missing (e.g. you say "book 2A" without a time), the bot replies with what it understood and asks for the rest.

---

## Tech Stack

- [Playwright](https://playwright.dev/python/) — browser automation
- [OpenAI GPT-4o-mini](https://platform.openai.com/) — natural language parsing
- [discord.py](https://discordpy.readthedocs.io/) — Discord bot framework
- [dateparser](https://dateparser.readthedocs.io/) / [python-dateutil](https://dateutil.readthedocs.io/) — date/time normalization
