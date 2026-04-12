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

# Single-thread executor: Playwright sync API must always run on the same thread.
# Must be recreated after closing a session because sync_playwright() taints the
# thread with a stale "running loop" that blocks future launches (Python 3.13+).
_browser_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


def _reset_browser_executor():
    """Shut down the old executor and create a fresh one with a clean thread."""
    global _browser_executor
    _browser_executor.shutdown(wait=False)
    _browser_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


async def _run_in_browser_thread(func, *args, **kwargs):
    """Run a blocking browser call on the dedicated browser thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _browser_executor, functools.partial(func, *args, **kwargs)
    )


class SessionControlView(discord.ui.View):
    """Reusable session controls for mobile-friendly Discord flows."""

    def __init__(self, cog: "PicklebotClient", user_id: int, allow_clear_pending: bool = False):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

        if not allow_clear_pending:
            self.remove_item(self.clear_pending)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This control bar belongs to another user.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Quit", style=discord.ButtonStyle.secondary)
    async def close_browser(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        async with self.cog.lock:
            await self.cog.close_browser_session(
                reason="Browser session closed from session controls.",
                clear_user_id=self.user_id,
            )
            self._disable_all_buttons()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                "🛑 Browser session closed. Use /ask anytime to start again.",
                ephemeral=True,
            )

    @discord.ui.button(label="Clear Pending", style=discord.ButtonStyle.primary)
    async def clear_pending(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        async with self.cog.lock:
            self.cog._pending.pop(self.user_id, None)
            self.cog._record_activity()
            button.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                "🧹 Cleared your pending request. You can send a fresh /ask command now.",
                ephemeral=True,
            )

    async def on_timeout(self) -> None:
        self._disable_all_buttons()

    def _disable_all_buttons(self) -> None:
        for child in self.children:
            child.disabled = True


class PostBookingView(discord.ui.View):
    """Buttons shown after a confirmed booking."""

    def __init__(self, cog: "PicklebotClient", user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This reservation prompt belongs to another user.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Book Another Time Slot", style=discord.ButtonStyle.primary)
    async def book_another(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        async with self.cog.lock:
            if automator is None:
                await interaction.response.send_message(
                    "Browser session is not available. Run /ask again to start a new reservation.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await _run_in_browser_thread(automator.create_another_reservation)

            if result.get("status") != "ready":
                await interaction.followup.send(
                    f"❌ {result.get('message', 'Could not prepare another reservation.')}",
                    ephemeral=True,
                )
                return

            self.cog._record_activity()
            self._disable_all_buttons()
            if interaction.message:
                await interaction.message.edit(view=self)
            await interaction.followup.send(
                "🎾 Ready for another reservation. Use /ask with your next booking command.",
                ephemeral=True,
            )

    @discord.ui.button(label="Quit", style=discord.ButtonStyle.secondary)
    async def quit_flow(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.close_browser_session(
            reason="User quit after booking.",
            clear_user_id=self.user_id,
        )
        self._disable_all_buttons()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "Reservation flow ended and the browser session was closed. Use /ask whenever you want to start again.",
            ephemeral=True,
        )

    async def on_timeout(self) -> None:
        self._disable_all_buttons()

    def _disable_all_buttons(self) -> None:
        for child in self.children:
            child.disabled = True


class PicklebotClient(commands.Cog):
    """Discord commands for Picklebot court automation."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lock = asyncio.Lock()  # Prevent concurrent command execution
        self._pending: dict[int, dict] = {}  # user_id -> partial instructions
        self._idle_timeout_seconds = self._load_idle_timeout_seconds()
        self._idle_task: Optional[asyncio.Task] = None
        self._activity_token = 0

    def _load_idle_timeout_seconds(self) -> int:
        """Load the browser idle timeout from the environment."""
        raw_value = os.getenv("COURT_BROWSER_IDLE_TIMEOUT_SECONDS", "300").strip()
        try:
            timeout = int(raw_value)
        except ValueError:
            logger.warning(
                "Invalid COURT_BROWSER_IDLE_TIMEOUT_SECONDS=%r; defaulting to 600 seconds.",
                raw_value,
            )
            return 600
        return max(0, timeout)

    def _record_activity(self) -> None:
        """Reset the idle timer whenever the browser session is used."""
        self._activity_token += 1

        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None

        if self._idle_timeout_seconds > 0:
            self._idle_task = asyncio.create_task(
                self._close_browser_after_idle(self._activity_token)
            )

    async def _close_browser_after_idle(self, token: int) -> None:
        """Close the browser session if no newer activity occurs before timeout."""
        try:
            await asyncio.sleep(self._idle_timeout_seconds)
            if token != self._activity_token:
                return

            async with self.lock:
                if token != self._activity_token:
                    return
                await self.close_browser_session(reason="Browser session closed after inactivity.")
        except asyncio.CancelledError:
            return

    async def close_browser_session(
        self,
        reason: Optional[str] = None,
        clear_user_id: Optional[int] = None,
    ) -> None:
        """Close the Playwright browser session and clear related state."""
        global automator, browser_session

        if clear_user_id is None:
            self._pending.clear()
        else:
            self._pending.pop(clear_user_id, None)

        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None

        if browser_session:
            try:
                await _run_in_browser_thread(browser_session.close)
                logger.info(reason or "Browser session closed")
            except Exception as e:
                logger.error(f"Error closing browser: {e}")
            finally:
                automator = None
                browser_session = None
                _reset_browser_executor()

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
            self._record_activity()
        except Exception as e:
            logger.error(f"Failed to initialize browser: {e}")
            # The failed sync_playwright().start() taints the thread's event
            # loop, so we must replace the executor to get a clean thread.
            automator = None
            browser_session = None
            _reset_browser_executor()
            raise

    async def cleanup_browser(self) -> None:
        """Clean up the browser session."""
        await self.close_browser_session(reason="Browser session closed")

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
        elif status == "completed":
            return (
                f"⚠️ **Booking Submitted, Verification Needed**\n"
                f"• Court: {result.get('court')}\n"
                f"• Date: {result.get('date')}\n"
                f"• Time: {result.get('time')}\n"
                f"• Note: {result.get('message', 'The booking flow completed, but confirmation was not detected.')}"
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
                normalized_question = question.strip().lower()
                if normalized_question in {"quit", "exit", "close", "close browser", "shutdown"}:
                    await self.close_browser_session(
                        reason="Browser session closed by user command.",
                        clear_user_id=interaction.user.id,
                    )
                    await interaction.followup.send(
                        "🛑 Browser session closed. The bot is still online; use /ask anytime to start again.",
                    )
                    return

                # Initialize browser if needed
                if automator is None:
                    await self.initialize_browser()

                self._record_activity()

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
                            self._record_activity()
                            await interaction.followup.send(
                                self._build_missing_feedback(merged),
                                view=SessionControlView(self, user_id, allow_clear_pending=True),
                            )
                            return
                    elif instructions.get("needs_more_info"):
                        # Store partial state and prompt for missing fields
                        partial = instructions.get("_partial", {})
                        self._pending[user_id] = {k: v for k, v in partial.items() if v is not None}
                        feedback = instructions.get("_feedback") or self._build_missing_feedback(instructions)
                        self._record_activity()
                        await interaction.followup.send(
                            f"🎾 {feedback}",
                            view=SessionControlView(self, user_id, allow_clear_pending=True),
                        )
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
                    self._record_activity()

                    formatted = self.format_availability_results(results, date)
                    await interaction.followup.send(
                        formatted,
                        view=SessionControlView(self, interaction.user.id),
                    )

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
                        self._record_activity()

                    await interaction.followup.send("📝 Processing booking...")
                    result = await _run_in_browser_thread(
                        automator.book_slot,
                        date, court, time, time_range,
                    )
                    self._record_activity()

                    formatted_result = self.format_booking_result(result)
                    if result.get("status") == "completed":
                        await interaction.followup.send(
                            formatted_result,
                            view=SessionControlView(self, interaction.user.id),
                        )
                    else:
                        await interaction.followup.send(formatted_result)

                    if result.get("status") == "booked":
                        await interaction.followup.send(
                            "Do you want to book another time slot or quit?",
                            view=PostBookingView(self, interaction.user.id),
                        )

            except Exception as e:
                logger.exception(f"Error processing command: {e}")

                # If the browser/page crashed, tear it down so the next
                # command gets a fresh session instead of staying broken.
                err_str = str(e)
                if any(sig in err_str for sig in ("Page crashed", "Target closed", "Target crashed", "Timeout", "ERR_ABORTED", "ERR_CONNECTION")):
                    logger.info("Detected browser crash/timeout – recycling session")
                    await self.close_browser_session(reason="Auto-recycled after browser failure")
                    await interaction.followup.send(
                        "⚠️ The browser session failed (crash or timeout). "
                        "A fresh session will start on your next command — please try again."
                    )
                else:
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
        picklebot_cog = bot.get_cog("PicklebotClient")
        if picklebot_cog:
            await picklebot_cog.cleanup_browser()


if __name__ == "__main__":
    asyncio.run(main())
