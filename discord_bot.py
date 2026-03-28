"""Discord bot wrapper for Picklebot court automation.

This bot allows you to check court availability and make bookings through Discord.
Natural language commands are processed via OpenAI.

Setup:
1. Create a Discord bot at https://discord.com/developers/applications
2. Copy your bot token to DISCORD_TOKEN in .env
3. Invite the bot to your server with 'Send Messages' and 'Slash Commands' permissions
4. Run: python discord_bot.py
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import os
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

from picklebot import agent, browser

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# Global session management
automator: Optional[browser.CourtAutomator] = None
browser_session: Optional[browser.BrowserSession] = None

# Single-thread executor: Playwright sync API must always run on the same thread
_browser_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


async def _run_in_browser_thread(func, *args, **kwargs):
    """Run a blocking browser call on the dedicated browser thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _browser_executor, functools.partial(func, *args, **kwargs)
    )


class PicklebotClient(commands.Cog):
    """Discord commands for Picklebot court automation."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lock = asyncio.Lock()  # Prevent concurrent command execution
        self._pending: dict[int, dict] = {}  # user_id -> partial instructions

    async def initialize_browser(self) -> None:
        """Initialize the persistent browser session."""
        global automator, browser_session

        if automator is not None:
            return  # Already initialized

        logger.info("Initializing Playwright browser session...")
        try:
            def _launch():
                global automator, browser_session
                browser_session = browser.launch(headless=False)
                automator = browser.CourtAutomator(browser_session)

            await _run_in_browser_thread(_launch)
            logger.info("Browser session initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize browser: {e}")
            raise

    async def cleanup_browser(self) -> None:
        """Clean up the browser session."""
        global automator, browser_session

        if browser_session:
            try:
                browser_session.close()
                automator = None
                browser_session = None
                logger.info("Browser session closed")
            except Exception as e:
                logger.error(f"Error closing browser: {e}")

    def format_availability_results(self, results: list, date: str = None) -> str:
        """Format availability results for Discord."""
        if not results:
            return "❌ No available slots found for your criteria."

        message = "📊 **Available Courts**"
        if date:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(date)
                date_label = dt.strftime("%A, %B %#d")
            except Exception:
                date_label = date
            message += f" — {date_label}"
        message += ":\n"
        for row in sorted(results, key=lambda r: r.get("court", "")):
            court = row.get("court", "Unknown")
            times = row.get("times", [])
            if times:
                slots = ", ".join(times)
                message += f"\n**Court {court}:** {slots}"
            else:
                message += f"\n**Court {court}:** No availability"

        return message

    def _build_missing_feedback(self, instr: dict) -> str:
        """Build a friendly message listing what was understood and what's still needed."""
        action = instr.get("action")
        date = instr.get("date")
        court = instr.get("court")
        time = instr.get("time") or instr.get("time_range")

        got = []
        if action:
            got.append(f"action: {action.replace('_', ' ')}")
        if date:
            got.append(f"date: {date}")
        if court:
            got.append(f"court: {court}")
        if time:
            got.append(f"time: {time}")

        missing = []
        if not action:
            missing.append("action — check availability or book?")
        if not date:
            missing.append("date — e.g. today, tomorrow, saturday")
        if action == "book" and not court:
            missing.append("court — 1A, 1B, 2A, or 2B")
        if action == "book" and not time:
            missing.append("time — e.g. 8pm, 8:30")

        parts = []
        if got:
            parts.append(f"Got: **{', '.join(got)}**")
        if missing:
            parts.append(f"Still need: **{', '.join(missing)}**")
        return " | ".join(parts) if parts else "I need more information to continue."

    def format_booking_result(self, result: dict) -> str:
        """Format a booking result."""
        status = result.get("status")
        if status == "booked":
            return (
                f"✅ **Booking Confirmed!**\n"
                f"• Court: {result.get('court')}\n"
                f"• Date: {result.get('date')}\n"
                f"• Time: {result.get('time')}"
            )
        else:
            return f"❌ **Booking Failed:** {result.get('message', 'Unknown error')}"

    @discord.app_commands.command(
        name="ask",
        description="Ask about court availability or make a booking in natural language",
    )
    @discord.app_commands.describe(
        question="What do you want to do? (e.g., 'what's available tomorrow at 8pm?', 'book court 1A on saturday')"
    )
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
        """Process a natural language court command."""
        async with self.lock:  # Prevent concurrent commands
            await interaction.response.defer()  # Show 'bot is thinking...'

            try:
                # Initialize browser if needed
                if automator is None:
                    await self.initialize_browser()

                # Process the command through OpenAI
                logger.info(f"Processing command: {question}")
                instructions = await asyncio.to_thread(agent.interpret_command, question)
                user_id = interaction.user.id

                # --- Multi-turn merge logic ---
                if instructions.get("valid"):
                    # Fully valid: clear any pending state and proceed
                    self._pending.pop(user_id, None)
                else:
                    if user_id in self._pending:
                        # Re-interpret using the full context (existing fields + new message)
                        pending = self._pending[user_id]
                        enriched = await asyncio.to_thread(
                            agent.interpret_command_with_context, question, pending
                        )
                        new_partial = enriched.get("_partial") or {
                            k: enriched.get(k)
                            for k in ("action", "date", "court", "time", "time_range")
                        }
                        merged = dict(pending)
                        for key, val in new_partial.items():
                            if val is not None:
                                merged[key] = val
                        merged["raw_command"] = question
                        instructions = agent.revalidate_instructions(merged)
                        if instructions.get("valid"):
                            del self._pending[user_id]
                        else:
                            self._pending[user_id] = {
                                k: merged.get(k)
                                for k in ("action", "date", "court", "time", "time_range")
                                if merged.get(k) is not None
                            }
                            await interaction.followup.send(self._build_missing_feedback(merged))
                            return
                    elif instructions.get("needs_more_info"):
                        # Store partial state and prompt for missing fields
                        partial = instructions.get("_partial", {})
                        self._pending[user_id] = {k: v for k, v in partial.items() if v is not None}
                        feedback = instructions.get("_feedback") or self._build_missing_feedback(instructions)
                        await interaction.followup.send(f"🎾 {feedback}")
                        return
                    else:
                        # Hard validation error (unrecognized action, etc.)
                        error = instructions.get("validation_error") or instructions.get("error")
                        await interaction.followup.send(f"❌ Could not understand: {error}")
                        return
                # --- End multi-turn merge logic ---

                action = instructions.get("action")
                date = instructions.get("date")
                court = instructions.get("court")
                time = instructions.get("time")
                time_range = instructions.get("time_range")

                # Handle check availability
                if action == "check_availability":
                    if not date and automator.on_availability_page:
                        date = automator.current_availability_date

                    # Login if needed
                    if not automator.on_availability_page:
                        try:
                            await _run_in_browser_thread(automator.login)
                        except Exception as e:
                            await interaction.followup.send(f"❌ Login failed: {e}")
                            return

                    await interaction.followup.send("🔍 Checking availability...")
                    results = await _run_in_browser_thread(
                        automator.check_availability,
                        date, court, time, time_range,
                    )

                    formatted = self.format_availability_results(results, date)
                    await interaction.followup.send(formatted)

                # Handle booking
                elif action == "book":
                    # Fill in date from availability page context if not provided
                    if not date and automator.on_availability_page:
                        date = automator.current_availability_date

                    # Check availability first if not on that page
                    if not automator.on_availability_page:
                        try:
                            await _run_in_browser_thread(automator.login)
                        except Exception as e:
                            await interaction.followup.send(f"❌ Login failed: {e}")
                            return

                        await _run_in_browser_thread(
                            automator.check_availability,
                            date, court, time, time_range,
                        )

                    await interaction.followup.send("📝 Processing booking...")
                    result = await _run_in_browser_thread(
                        automator.book_slot,
                        date, court, time, time_range,
                    )

                    formatted_result = self.format_booking_result(result)
                    await interaction.followup.send(formatted_result)

            except Exception as e:
                logger.exception(f"Error processing command: {e}")
                await interaction.followup.send(
                    f"❌ An error occurred: {str(e)[:200]}"
                )


async def setup_bot() -> commands.Bot:
    """Create and configure the Discord bot."""
    intents = discord.Intents.default()

    bot = commands.Bot(
        command_prefix="/",
        intents=intents,
    )

    @bot.event
    async def on_ready() -> None:
        logger.info(f"Logged in as {bot.user}")
        try:
            guild_id = os.getenv("DISCORD_GUILD_ID")
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                logger.info(f"Synced {len(synced)} command(s) to guild {guild_id}")
            else:
                synced = await bot.tree.sync()
                logger.info(f"Synced {len(synced)} command(s) globally (may take up to 1 hour)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

    # Add the cog
    await bot.add_cog(PicklebotClient(bot))

    return bot


async def main() -> None:
    """Entry point for the Discord bot."""
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("DISCORD_TOKEN not found in .env file")
        raise ValueError(
            "Please set DISCORD_TOKEN in your .env file. "
            "Get it from https://discord.com/developers/applications"
        )

    bot = await setup_bot()

    try:
        await bot.start(token)
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")
    finally:
        # Cleanup
        global browser_session
        if browser_session:
            browser_session.close()


if __name__ == "__main__":
    asyncio.run(main())
