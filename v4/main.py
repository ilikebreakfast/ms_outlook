"""
v4 pipeline entry point.

Run from the ms_outlook/ repo root or from inside v4/:

    python v4/main.py                   # one poll cycle (default)
    python v4/main.py --schedule        # APScheduler daemon
    python v4/main.py --check-auth      # verify app-only token + one Graph read
    python v4/main.py --review          # launch v3 interactive staging review

IT prerequisites before first run:
  1. Admin consent for Mail.Read (Application permission) on the app registration.
  2. New-ApplicationAccessPolicy restricting the app to the shared mailbox only.
  3. Certificate generated and uploaded; private key PEM at MS_CERT_PATH.
  4. All .env values filled in (copy from v4/.env.example).
"""
import argparse
import logging
import sys
from pathlib import Path

# --- sys.path (must precede domain imports) ---
_V4_ROOT   = Path(__file__).resolve().parent
_REPO_ROOT = _V4_ROOT.parent
_V3_ROOT   = _REPO_ROOT / "v3"
for _p in (str(_V3_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("v4")


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_check_auth() -> None:
    """Acquire an app-only token and read one message from the shared mailbox."""
    from v4.core.graph_client import get_access_token, GraphClient
    from v4.core.config import SHARED_MAILBOX_UPN

    log.info("Acquiring app-only token…")
    token = get_access_token()
    log.info("Token acquired (len=%d)", len(token))

    client = GraphClient()
    resp   = client.get(
        f"/users/{SHARED_MAILBOX_UPN}/mailFolders/Inbox/messages"
        "?$top=1&$select=id,subject,receivedDateTime"
    )
    msgs = resp.get("value", [])
    log.info("Shared mailbox read OK — %d message(s) returned", len(msgs))
    if msgs:
        log.info("Latest subject: %s  (%s)", msgs[0].get("subject", "(no subject)"),
                 msgs[0].get("receivedDateTime", ""))
    log.info("Auth check passed.")


def cmd_once() -> None:
    """Run one poll cycle and exit."""
    from v4.core.intake import init_db
    from v4.pipeline import run_pipeline

    init_db()
    summary = run_pipeline()
    log.info("Cycle summary: %s", summary)


def cmd_schedule() -> None:
    """Run on a repeating schedule using APScheduler (blocking)."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError:
        log.error("APScheduler not installed.  Run: pip install apscheduler")
        sys.exit(1)

    from v4.core.config import POLL_INTERVAL_MINUTES
    from v4.core.intake import init_db
    from v4.pipeline import run_pipeline

    init_db()
    scheduler = BlockingScheduler()
    scheduler.add_job(run_pipeline, "interval", minutes=POLL_INTERVAL_MINUTES,
                      id="poll_and_process")
    log.info("Scheduler started — polling every %d minute(s).  Ctrl+C to stop.",
             POLL_INTERVAL_MINUTES)
    try:
        # Run immediately on startup, then on the interval
        run_pipeline()
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


def cmd_review() -> None:
    """Launch the v3 interactive staging review CLI."""
    import runpy
    review_path = _V3_ROOT / "core" / "invoices" / "manual_review.py"
    if not review_path.exists():
        log.error("v3 manual_review.py not found at %s", review_path)
        sys.exit(1)
    runpy.run_path(str(review_path), run_name="__main__")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="v4 invoice pipeline — app-only shared mailbox intake",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check-auth", action="store_true",
        help="Verify app-only Graph auth and read one message from the shared mailbox",
    )
    group.add_argument(
        "--once", action="store_true",
        help="Run one poll cycle and exit (default when no flag given)",
    )
    group.add_argument(
        "--schedule", action="store_true",
        help="Run on a repeating schedule (POLL_INTERVAL_MINUTES from .env)",
    )
    group.add_argument(
        "--review", action="store_true",
        help="Launch the v3 interactive staging review CLI",
    )
    args = parser.parse_args()

    if args.check_auth:
        cmd_check_auth()
    elif args.schedule:
        cmd_schedule()
    elif args.review:
        cmd_review()
    else:
        # --once is the default
        cmd_once()


if __name__ == "__main__":
    main()
