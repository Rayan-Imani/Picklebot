"""Browser automation helpers using Playwright."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


@dataclass
class BrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    xvfb_process: Optional[subprocess.Popen] = None
    xvfb_display: Optional[str] = None

    def close(self) -> None:
        """Cleanly close the Playwright session.

        This method is intentionally tolerant of errors, since the browser may
        already have been closed by the underlying Playwright runtime.
        """
        for cleanup in (self.context.close, self.browser.close, self.playwright.stop):
            try:
                cleanup()
            except Exception:
                # Best-effort cleanup; ignore any errors during shutdown.
                pass

        if self.xvfb_process:
            try:
                self.xvfb_process.terminate()
                self.xvfb_process.wait(timeout=3)
            except Exception:
                try:
                    self.xvfb_process.kill()
                except Exception:
                    pass

        if self.xvfb_display and os.getenv("DISPLAY") == self.xvfb_display:
            os.environ.pop("DISPLAY", None)


def _display_socket_exists(display: Optional[str]) -> bool:
    """Return whether the X11 socket for a DISPLAY appears to exist."""
    if not display or not display.startswith(":"):
        return False

    display_number = display[1:].split(".", 1)[0]
    if not display_number.isdigit():
        return False

    socket_path = f"/tmp/.X11-unix/X{display_number}"
    return os.path.exists(socket_path)


def _ensure_virtual_display(headless: bool) -> Optional[subprocess.Popen]:
    """Start Xvfb when running headed Chromium without an existing display."""
    current_display = os.getenv("DISPLAY")
    if headless:
        return None

    if current_display and _display_socket_exists(current_display):
        return None

    if current_display and not _display_socket_exists(current_display):
        os.environ.pop("DISPLAY", None)

    xvfb_path = shutil.which("Xvfb")
    if not xvfb_path:
        return None

    display = os.getenv("COURT_XVFB_DISPLAY", ":99")
    process = subprocess.Popen(
        [
            xvfb_path,
            display,
            "-screen",
            "0",
            "1440x2200x24",
            "-ac",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    os.environ["DISPLAY"] = display
    time.sleep(0.5)
    return process


def launch(headless: bool = False) -> BrowserSession:
    """Launch a visible Playwright browser and return a session object."""
    raw_headless = os.getenv("COURT_HEADLESS")
    if raw_headless is not None:
        headless = raw_headless.strip().lower() in {"1", "true", "yes", "on"}

    xvfb_process = _ensure_virtual_display(headless)

    playwright = sync_playwright().start()

    # Use common flags that make the browser a bit less bot-like and stable
    # in containerised environments.
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",   # Avoid /dev/shm (tiny in Docker)
        "--no-sandbox",              # Required in most containers
        "--disable-gpu",             # No GPU in containers
        "--disable-extensions",
    ]
    if not headless:
        launch_args.append("--start-maximized")

    try:
        browser = playwright.chromium.launch(
            headless=headless,
            args=launch_args,
        )
    except Exception:
        # Clean up the Playwright instance so the thread's event loop isn't
        # left in a broken state (avoids "async in loop" on retry).
        try:
            playwright.stop()
        except Exception:
            pass
        raise

    context_kwargs = {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "locale": "en-US",
        "timezone_id": os.getenv("COURT_TIMEZONE", "America/Chicago"),
        "extra_http_headers": {
            "accept-language": "en-US,en;q=0.9",
        },
    }
    if headless:
        context_kwargs["viewport"] = {"width": 1440, "height": 2200}
    else:
        context_kwargs["no_viewport"] = True

    context = browser.new_context(**context_kwargs)

    page = context.new_page()
    # Reduce webdriver fingerprint.
    page.add_init_script(
        """Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"""
    )

    # Start on a simple blank page. The agent can navigate from here.
    page.goto("about:blank")

    return BrowserSession(
        playwright=playwright,
        browser=browser,
        context=context,
        page=page,
        xvfb_process=xvfb_process,
        xvfb_display=os.getenv("DISPLAY") if xvfb_process else None,
    )


class CourtAutomator:
    """High-level helpers to automate a court reservation website.

    This is intentionally generic: you should update `COURT_*` environment
    variables to match your reservation site's URLs and selectors.
    """

    def __init__(self, session: BrowserSession):
        self.session = session
        self.page = session.page

        # State tracking for persistent sessions
        self.on_availability_page = False
        self.current_availability_date = None
        self.current_availability_court = None

        # Configuration defaults. Override via environment variables.
        self.config: Dict[str, Optional[str]] = {
            # The landing URL for the reservation system.
            "site_url": os.getenv("COURT_SITE_URL"),
            "login_url": os.getenv("COURT_LOGIN_URL"),
            "username": os.getenv("COURT_USERNAME"),
            "password": os.getenv("COURT_PASSWORD"),
            # Basic selectors for login flow.
            # Default values match the login inputs on the provided site.
            "login_username_selector": os.getenv(
                "COURT_LOGIN_USERNAME_SELECTOR", "#login"
            ),
            "login_password_selector": os.getenv(
                "COURT_LOGIN_PASSWORD_SELECTOR", "#password"
            ),
            "login_submit_selector": os.getenv(
                "COURT_LOGIN_SUBMIT_SELECTOR", "button[type='submit']"
            ),
            # Page/selector for availability.
            "availability_page_url": os.getenv("COURT_AVAILABILITY_URL"),
            "availability_table_selector": os.getenv("COURT_AVAILABILITY_TABLE_SELECTOR"),
        }

    def _require_config(self, key: str) -> str:
        value = self.config.get(key)
        if not value:
            raise RuntimeError(
                f"Missing configuration: {key}.\n"
                "Set the corresponding COURT_* environment variable in your .env."
            )
        return value

    def _goto(self, url: str, retries: int = 2) -> None:
        """Navigate to *url* with automatic retry on transient failures.

        Chromium can raise ``net::ERR_ABORTED`` when a post-login redirect
        race-conditions with a fresh ``goto``, or when the network blips
        in a long-running container.  Retrying with a short pause and a
        more lenient ``wait_until`` usually resolves the issue.
        """
        for attempt in range(1, retries + 2):  # 1-indexed, total = retries + 1
            try:
                self.page.goto(url, wait_until="domcontentloaded")
                return
            except Exception as exc:
                if attempt > retries:
                    raise
                err_text = str(exc)
                if "ERR_ABORTED" in err_text or "ERR_CONNECTION" in err_text:
                    self.page.wait_for_timeout(1500)
                    continue
                raise

    def login(self) -> None:
        """Log in to the reservation system.

        If `COURT_LOGIN_URL` is not set, we will fall back to `COURT_SITE_URL`.
        If required credentials/selectors are missing, this will skip login.
        """
        # Determine which URL to use for login.
        login_url = self.config.get("login_url") or self.config.get("site_url")
        if not login_url:
            print(
                "[WARN] Missing configuration: login_url or site_url. "
                "Skipping login."
            )
            return

        username = self.config.get("username")
        password = self.config.get("password")
        username_sel = self.config.get("login_username_selector")
        password_sel = self.config.get("login_password_selector")
        submit_sel = self.config.get("login_submit_selector")

        if not all([username, password, username_sel, password_sel, submit_sel]):
            print(
                "[WARN] Missing login configuration (username/password/selectors). "
                "Skipping login."
            )
            return

        self._goto(login_url)
        self.page.fill(username_sel, username)
        self.page.fill(password_sel, password)
        self.page.click(submit_sel)
        self.page.wait_for_load_state("networkidle")
        self._logged_in = True

    def _select_location(self, retries: int = 2) -> None:
        """Select the Shawnee Trail location on the reservation page."""
        self._enable_graphql_auth_interception()
        last_exc = None
        for attempt in range(1, retries + 2):
            try:
                frame = self._get_frame()
                location_select = frame.get_by_test_id("location-select")
                location_select.wait_for(state="visible", timeout=30000)
                location_select.select_option("49")
                return
            except Exception as exc:
                last_exc = exc
                if attempt > retries:
                    raise
                print(
                    f"[WARN] _select_location attempt {attempt} failed: {exc}. "
                    "Reloading page and retrying…"
                )
                # Reload the page so the iframe is re-injected from scratch.
                try:
                    self.page.reload(wait_until="networkidle")
                    self.page.wait_for_timeout(2000)
                except Exception:
                    pass

    def _extract_bearer_from_storage(self) -> Optional[str]:
        """Look for a JWT-like token in localStorage/sessionStorage."""
        script = r"""
        (() => {
          const maybe = (store) => {
            for (const key of Object.keys(store)) {
              const value = store.getItem(key);
              if (typeof value === 'string' && /^([A-Za-z0-9_-]+\.){2}[A-Za-z0-9_-]+$/.test(value)) {
                return value;
              }
            }
            return null;
          };
          return maybe(window.localStorage) || maybe(window.sessionStorage) || null;
        })();
        """
        try:
            token = self.page.evaluate(script)
            if token:
                return token
        except Exception:
            return None
        return None

    def _enable_graphql_auth_interception(self) -> None:
        """Intercept GraphQL calls and ensure they include an Authorization header."""
        token = self._extract_bearer_from_storage()
        if not token:
            return

        def handle_route(route, request):
            headers = dict(request.headers)
            if "authorization" not in {k.lower() for k in headers}:
                headers["authorization"] = f"Bearer {token}"
            route.continue_(headers=headers)

        self.page.route("**/graphql", handle_route)

    def _get_frame(self, timeout: int = 30000):
        """Get the content frame from the Member Portal iframe."""
        # Use page.wait_for_selector (more reliable for iframes than
        # locator.wait_for, which can time-out despite resolving the element).
        self.page.wait_for_selector(
            'iframe[title="Member Portal"]', state="attached", timeout=timeout
        )
        frame = self.page.frame_locator('iframe[title="Member Portal"]')
        # Wait until the iframe's own content has rendered at least one element
        # so that subsequent interactions don't race against the iframe load.
        try:
            frame.locator("body").wait_for(state="attached", timeout=timeout)
        except Exception:
            pass  # best-effort; some cross-origin frames don't expose body
        return frame

    def _select_court(self, court_value: str) -> None:
        """Select a specific court in the court dropdown."""
        court_map = {
            "1A": "182",
            "2A": "183",
            "1B": "301",
            "2B": "302",
        }
        option = court_map.get(court_value.upper())
        if option:
            frame = self._get_frame()
            frame.get_by_test_id("resource-select").select_option(option)

    def _scroll_and_click(self, locator, timeout: int = 5000) -> None:
        """Ensure a locator is visible in the viewport before clicking it."""
        locator.wait_for(state="visible", timeout=timeout)
        locator.scroll_into_view_if_needed(timeout=timeout)
        locator.click()

    def _click_view_availability(self) -> None:
        """Click the "View Availability" button."""
        frame = self._get_frame()
        self._scroll_and_click(frame.get_by_test_id("view-availability-button"))

    def _ensure_evening_tab(self) -> None:
        """Click the Evening filter button if it isn't already selected."""
        frame = self._get_frame()
        try:
            evening_btn = frame.get_by_role("button", name="Evening")
            evening_btn.wait_for(state="visible", timeout=5000)
            # The button uses aria-pressed or a class to indicate active state;
            # check both common patterns before clicking.
            is_pressed = evening_btn.get_attribute("aria-pressed")
            class_attr = evening_btn.get_attribute("class") or ""
            already_active = is_pressed == "true" or "active" in class_attr or "selected" in class_attr
            if not already_active:
                self._scroll_and_click(evening_btn)
                self.page.wait_for_timeout(400)
        except Exception:
            # Evening tab not present on this page — ignore.
            pass

    def _pick_date(self, date_iso: str) -> None:
        """Pick a date in the calendar widget, navigating to the correct month first."""
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(date_iso)
            target_month = dt.strftime("%B")   # e.g. "April"
            button_name = dt.strftime("%B %d")  # e.g. "April 04"
            frame = self._get_frame()

            import calendar as _cal
            month_names = list(_cal.month_name)[1:]  # ["January", ..., "December"]

            for _ in range(12):  # Safety cap
                # Find which month is currently shown in the calendar header.
                current_month = None
                for m in month_names:
                    try:
                        if frame.get_by_text(m).first.is_visible(timeout=300):
                            current_month = m
                            break
                    except Exception:
                        continue

                if current_month == target_month:
                    break

                # Click the Next button to advance one month forward.
                try:
                    self._scroll_and_click(frame.get_by_role("button").filter(has_text="Next"))
                    self.page.wait_for_timeout(600)
                except Exception:
                    break

            # Now click the target day button.
            self._scroll_and_click(frame.get_by_role("button", name=button_name))
        except Exception as e:
            print(f"[WARN] _pick_date failed for '{date_iso}': {e}")

    def _gather_available_times(self) -> List[str]:
        """Collect available time buttons from the currently displayed calendar.
        
        If no time slots are available, returns an empty list.
        """
        self.page.wait_for_timeout(800)
        frame = self._get_frame()
        
        # Check if the "no available time slots" message is present
        try:
            no_slots_message = frame.get_by_text("There are no available time slots").is_visible(timeout=500)
            if no_slots_message:
                return []
        except Exception:
            # Message not found, proceed to collect buttons
            pass
        
        buttons = frame.locator(
            "button.available-times-styles__AvailableTimeItem-sc-583c8816-2"
        ).all()
        return [b.inner_text().strip() for b in buttons if b.inner_text().strip()]

    def check_availability(
        self,
        date: str,
        court: Optional[str] = None,
        time: Optional[str] = None,
        time_range: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Check availability and return a list of available slots.

        Returns a list of records like:
          {"court": "1A", "times": ["18:00", "19:30"]}
        
        After this completes, the browser remains on the availability page
        so booking can continue from here without reloading.
        """
        # Navigate to site only if we haven't just logged in (login already
        # lands on the site).  This avoids a redundant goto that can trigger
        # ERR_ABORTED when the site issues post-login redirects.
        site_url = self.config.get("site_url")
        if site_url and not getattr(self, "_logged_in", False):
            self._goto(site_url)
            self.page.wait_for_load_state("networkidle")
        # Reset the flag so future calls navigate normally.
        self._logged_in = False

        # Click the Reserve a Court link.
        try:
            self.page.click("#menu_reserve_a_court")
            self.page.wait_for_load_state("networkidle")
        except Exception:
            pass

        # Select Shawnee Trail location.
        try:
            self._select_location()
        except Exception as e:
            print(f"[ERROR] Failed to select location: {e}")
            raise RuntimeError(f"Could not load the court reservation page: {e}")

        courts_to_check = [court] if court else ["1A", "2A", "1B", "2B"]
        results: List[Dict[str, Any]] = []

        for c in courts_to_check:
            self._select_court(c)
            self._click_view_availability()
            # Give the page a moment to render availability.
            self.page.wait_for_timeout(800)

            # Click Evening tab to reveal evening slots.
            self._ensure_evening_tab()

            # Select the desired date.
            self._pick_date(date)
            self.page.wait_for_timeout(800)

            times = self._gather_available_times()
            results.append({"court": c, "times": times})

        # Mark that we're now on the availability page and can book from here
        self.on_availability_page = True
        self.current_availability_date = date
        self.current_availability_court = courts_to_check[-1]  # Last court checked

        return results

    def book_slot(
        self,
        date: str,
        court: str,
        time: Optional[str] = None,
        time_range: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Book a slot from the current availability page.

        This method assumes the availability page is already displayed from a 
        previous check_availability call. It will:
        1. Select the court (if different from current)
        2. Select the time slot matching the requested time
        3. Click the book/reserve button
        4. Confirm the reservation

        Returns the booking result with status, message, and confirmation details.
        """
        if not self.on_availability_page:
            return {
                "status": "error",
                "message": "Not on availability page. Run check_availability first.",
                "requested": {
                    "date": date,
                    "court": court,
                    "time": time,
                    "time_range": time_range,
                },
            }

        try:
            frame = self._get_frame()

            # If requesting a different court, select it
            if court.upper() != self.current_availability_court.upper():
                self._select_court(court)
                self._click_view_availability()
                self.page.wait_for_timeout(800)
                self._pick_date(date)
                self.page.wait_for_timeout(800)
                self.current_availability_court = court

            # If date is different, pick it
            if date != self.current_availability_date:
                self._pick_date(date)
                self.page.wait_for_timeout(800)
                self.current_availability_date = date

            # Find and click the matching time slot
            times = self._gather_available_times()
            selected_time = None

            if time:
                requested_candidates = self._candidate_times_for_matching(time)
                for available_time in times:
                    normalized_available = self._normalize_time_for_matching(available_time)
                    if normalized_available in requested_candidates:
                        selected_time = available_time
                        break
            elif time_range:
                # For time ranges, pick the first available slot
                selected_time = times[0] if times else None

            if not selected_time:
                tried = ", ".join(self._candidate_times_for_matching(time)) if time else time_range
                available_display = ", ".join(times) if times else "none"
                return {
                    "status": "no_availability",
                    "message": f"No available times matching {time or time_range} (tried: {tried}; available: {available_display})",
                    "requested": {
                        "date": date,
                        "court": court,
                        "time": time,
                        "time_range": time_range,
                    },
                }

            # Click the selected time button
            button = frame.get_by_role("button", name=selected_time)
            self._scroll_and_click(button)
            self.page.wait_for_timeout(500)

            # Look for and click the reservation button (could be "Start", "Reserve", "Book", etc.)
            try:
                reserve_button = frame.get_by_role("button", name="Start").or_(
                    frame.get_by_role("button", name="Reserve")
                ).or_(
                    frame.get_by_role("button", name="Book")
                ).or_(
                    frame.get_by_test_id("reserveResourcePreviewReservation")
                )
                self._scroll_and_click(reserve_button)

                self.page.wait_for_timeout(400)
            except Exception as e:
                print(f"[WARNING] Could not click reservation button: {e}")
                pass

            # Look for and click the "Pay Now" button
            try:
                pay_button = frame.get_by_test_id("pay-button").or_(
                    frame.get_by_role("button", name="Pay Now")
                )
                self._scroll_and_click(pay_button)
                self.page.wait_for_timeout(600)
            except Exception as e:
                print(f"[WARNING] Could not click pay button: {e}")
                pass

            # Try to confirm any confirmation dialogs
            try:
                confirm_button = frame.get_by_role("button", name="Confirm").or_(
                    frame.get_by_role("button", name="Yes")
                )
                self._scroll_and_click(confirm_button)
                self.page.wait_for_timeout(500)
            except Exception:
                pass

            booking_successful = self._detect_booking_success()

            # Distinguish between confirmed success and a best-effort completion
            # where the site did not show a success banner we can verify.
            status = "booked" if booking_successful else "completed"
            message = (
                f"Successfully booked court {court} on {date} at {selected_time}"
                if booking_successful
                else (
                    f"Booking flow completed for court {court} on {date} at {selected_time}, "
                    "but the success confirmation was not detected. Please verify on the reservation site."
                )
            )

            return {
                "status": status,
                "message": message,
                "court": court,
                "date": date,
                "time": selected_time,
            }

        except Exception as e:
            return {
                "status": "error",
                "message": f"Booking failed: {str(e)}",
                "requested": {
                    "date": date,
                    "court": court,
                    "time": time,
                    "time_range": time_range,
                },
            }

    def create_another_reservation(self) -> Dict[str, Any]:
        """Return from a completed booking to the reservation flow."""
        try:
            frame = self._get_frame()
            button = frame.get_by_test_id("createAnotherReservationButton").or_(
                frame.get_by_role("button", name="Create Another Reservation")
            ).or_(
                frame.get_by_text("Create Another Reservation")
            )
            self._scroll_and_click(button)
            self.page.wait_for_load_state("networkidle")
            self.page.wait_for_timeout(800)

            self.on_availability_page = True

            return {
                "status": "ready",
                "message": "Ready to create another reservation.",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Could not reopen the reservation page: {str(e)}",
            }

    def _detect_booking_success(self) -> bool:
        """Check for a confirmed booking success message after submission."""
        for timeout in (1500, 3000):
            try:
                frame = self._get_frame(timeout=timeout)
                success_message = frame.get_by_text("Booking Successful!", exact=True)
                success_message.wait_for(state="visible", timeout=timeout)
                print("[SUCCESS] Booking Successful! message found in frame")
                return True
            except Exception:
                pass

            try:
                success_message = self.page.get_by_text("Booking Successful!", exact=True)
                success_message.wait_for(state="visible", timeout=timeout)
                print("[SUCCESS] Booking Successful! message found on page")
                return True
            except Exception:
                pass

        return False

    def _normalize_time_for_matching(self, time_str: str) -> str:
        """Normalize time string for matching (e.g., '8:00 PM' -> '20:00')."""
        try:
            from datetime import datetime
            # Clean up the string first - remove extra whitespace
            clean_str = time_str.strip()
            
            # Try 12-hour format with AM/PM (various formats)
            for fmt in ["%I:%M %p", "%I:%M%p", "%I%M %p", "%I%M%p"]:
                try:
                    dt = datetime.strptime(clean_str, fmt)
                    return dt.strftime("%H:%M")
                except ValueError:
                    continue
            
            # Try 24-hour format
            for fmt in ["%H:%M", "%H%M"]:
                try:
                    dt = datetime.strptime(clean_str, fmt)
                    return dt.strftime("%H:%M")
                except ValueError:
                    continue
                    
            # If nothing works, return as-is
            return clean_str
        except Exception:
            return time_str.strip()

    def _candidate_times_for_matching(self, time_str: str) -> List[str]:
        """Build plausible normalized times for ambiguous user input.

        Availability is gathered from the evening view, so an ambiguous input like
        "9:30" should also try matching "21:30" when no AM/PM is provided.
        """
        normalized = self._normalize_time_for_matching(time_str)
        candidates = [normalized]

        try:
            if ":" not in normalized:
                return candidates

            hour_text, minute_text = normalized.split(":", 1)
            hour = int(hour_text)
            if 1 <= hour <= 11:
                candidates.append(f"{hour + 12:02d}:{minute_text}")
        except Exception:
            return candidates

        return list(dict.fromkeys(candidates))
