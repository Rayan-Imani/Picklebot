"""Entry point for the Picklebot automation tool.

This script launches a visible Playwright browser and provides a simple CLI for
entering natural language commands.
"""

from __future__ import annotations

from picklebot import agent, browser


def _format_instructions(instructions: dict) -> str:
    """Format interpreted instructions into a friendly message."""
    action = instructions.get("action")
    date = instructions.get("date")
    court = instructions.get("court")
    time = instructions.get("time")
    time_range = instructions.get("time_range")
    
    if action == "check_availability":
        msg = "Checking for available courts"
        if date:
            msg += f" on {date}"
        if court:
            msg += f" (court {court})"
        if time:
            msg += f" at {time}"
        elif time_range:
            msg += f" between {time_range}"
        return msg
    
    elif action == "book":
        timing = time or time_range or "unknown time"
        return f"Booking court {court} on {date} at {timing}"
    
    return f"Performing action: {action}"


def _confirm(prompt: str) -> bool:
    """Ask the user to confirm a yes/no decision."""
    response = input(f"{prompt} [Yes/No]: ").strip().lower()
    return response in {"y", "yes"}


def main() -> None:
    print("PickleMan - Running")
    print("Type 'exit' or 'quit' to end the session.\n")

    session = browser.launch()  # Launch a Playwright browser session
    automator = browser.CourtAutomator(session)

    try:
        while True:
            command = input("Enter a command: ").strip()
            if not command:
                continue

            if command.lower() in {"exit", "quit", "q"}:
                print("Exiting Picklebot.")
                break

            instructions = agent.interpret_command(command)

            if not instructions.get("valid", False):
                friendly_msg = _format_instructions(instructions)
                print(f"\n{friendly_msg}")
                error = instructions.get("validation_error") or instructions.get("error")
                print(f"\nCould not interpret command: {error}")
                print("Please rephrase the request.\n")
                continue

            action = instructions.get("action")
            date = instructions.get("date")
            court = instructions.get("court")
            time = instructions.get("time")
            time_range = instructions.get("time_range")

            # If no date provided but we're on the availability page, use the current date
            if not date and automator.on_availability_page:
                date = automator.current_availability_date

            # Only login if we're not already on the availability page
            # (checking availability requires login; booking from current page doesn't)
            if not automator.on_availability_page:
                try:
                    automator.login()
                except Exception as exc:
                    print(f"Failed to login: {exc}")
                    break

            if action == "check_availability":
                results = automator.check_availability(
                    date=date,
                    court=court,
                    time=time,
                    time_range=time_range,
                )
                print("\nAvailability results:")
                for row in results:
                    print(row)

            elif action == "book":
                # If already on availability page, book directly from there
                # Otherwise, check availability first then book
                if not automator.on_availability_page:
                    print("Checking availability first...")
                    automator.check_availability(
                        date=date,
                        court=court,
                        time=time,
                        time_range=time_range,
                    )

                timing = time or time_range
                prompt = (
                    f"About to book court {court} on {date} at {timing}. Proceed?"
                )
                if not _confirm(prompt):
                    print("Booking canceled.\n")
                    continue

                result = automator.book_slot(
                    date=date,
                    court=court,
                    time=time,
                    time_range=time_range,
                )
                print("\nBooking result:")
                status = result.get("status")
                message = result.get("message")
                print(f"Status: {status}")
                print(f"Message: {message}")
                if status == "booked":
                    print(f"Court: {result.get('court')}")
                    print(f"Date: {result.get('date')}")
                    print(f"Time: {result.get('time')}")
                    print("\n✓ Result booked successfully!")
                    
                    # Ask if they want to do anything else
                    if not _confirm("\nWould you like to book another court?"):
                        print("Thank you for using PickleMan! Type 'exit' to quit the session.")
                    else:
                        print()  # Just print a newline to separate from next command
                elif status == "completed":
                    print(f"Court: {result.get('court')}")
                    print(f"Date: {result.get('date')}")
                    print(f"Time: {result.get('time')}")
                    print("\n! Booking flow completed, but success could not be verified automatically.")

            else:
                print("Unknown action. Nothing to do.")

            print()

    finally:
        session.close()


if __name__ == "__main__":
    main()
