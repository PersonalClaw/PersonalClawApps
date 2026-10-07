"""CLI setup and doctor commands for Slack app."""

import sys
from pathlib import Path

from personalclaw.sdk.provider import ProviderSettings
from slack_runtime.settings import APP, migrate_from_core

APP_NAME = "slack"


def main():
    """Main setup entry point."""
    # Run migration if needed
    migrate_from_core()
    
    # Import setup logic
    from slack_runtime.setup import run_setup
    
    run_setup()


def doctor():
    """Run doctor checks."""
    print(f"Checking {APP} app configuration...")
    
    settings = ProviderSettings.load(APP)
    
    checks = []
    
    # Check tokens
    if settings.get("bot_token"):
        checks.append(("Bot token", "✓"))
    else:
        checks.append(("Bot token", "✗ Missing"))
    
    if settings.get("app_token"):
        checks.append(("App token", "✓"))
    else:
        checks.append(("App token", "✗ Missing"))
    
    # Check channel settings
    if settings.get("allowed_users"):
        checks.append(("Allowed users", f"✓ ({len(settings['allowed_users'])} users)"))
    else:
        checks.append(("Allowed users", "○ Not configured"))
    
    print("\nSlack Configuration Status:")
    for check_name, status in checks:
        print(f"  {check_name}: {status}")
    
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        sys.exit(doctor())
    else:
        main()
