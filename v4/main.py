"""
v4 pipeline entry point.

Usage (run from ms_outlook/ repo root or from inside v4/):

    python v4/main.py                   # one poll cycle (default)
    python v4/main.py --schedule        # APScheduler daemon
    python v4/main.py --check-auth      # verify app-only token + shared mailbox read
    python v4/main.py --review          # launch v3 interactive staging review

IT prerequisites before first run (see v4/.env.example for details):
  1. Admin consent for Mail.Read (Application permission) on the app registration.
  2. New-ApplicationAccessPolicy restricting the app to the shared mailbox only.
  3. Certificate generated, public half uploaded; private key PEM at MS_CERT_PATH.
  4. All .env values filled in (copy from v4/.env.example).
"""
import argparse
import logging
import sys
from datetime import datetime
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

_SEP  = "  " + "─" * 52
_SEP2 = "  " + "━" * 52


def _banner(title: str) -> None:
    log.info("\n%s\n  %s\n%s", _SEP, title, _SEP)


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_check_auth() -> None:
    """Verify app-only Graph auth and print a health summary."""
    from v4.core.config import startup_check, SHARED_MAILBOX_UPN, CERT_PATH
    startup_check("auth")

    _banner("v4 pipeline — auth check")

    # 1. Token acquisition
    log.info("  Acquiring app-only token…")
    try:
        from v4.core.graph_client import get_access_token
        token = get_access_token()
        log.info("  ✓  Token acquired  (len=%d)", len(token))
    except Exception as e:
        log.error("  ✗  Token acquisition failed: %s", e)
        log.error("\n  Possible causes:")
        log.error("    • MS_CERT_PATH points to a file that doesn't exist or isn't a valid PEM")
        log.error("    • MS_CLIENT_ID / MS_TENANT_ID are wrong")
        log.error("    • The certificate hasn't been uploaded to the app registration")
        log.error("    • Admin consent for Mail.Read hasn't been granted")
        sys.exit(1)

    # 2. Shared mailbox read
    log.info("  Reading shared mailbox:  %s", SHARED_MAILBOX_UPN)
    try:
        from v4.core.graph_client import GraphClient
        client = GraphClient()
        resp   = client.get(
            f"/users/{SHARED_MAILBOX_UPN}/mailFolders/Inbox/messages"
            "?$top=1&$select=id,subject,receivedDateTime,from"
        )
        msgs = resp.get("value", [])
        log.info("  ✓  Shared mailbox read OK")
        if msgs:
            m = msgs[0]
            sndr = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
            log.info("     Latest message: %r  from=%s  (%s)",
                     m.get("subject", "(no subject)"), sndr,
                     m.get("receivedDateTime", "?")[:10])
        else:
            log.info("     (inbox appears empty)")
    except Exception as e:
        log.error("  ✗  Shared mailbox read failed: %s", e)
        log.error("\n  Possible causes:")
        log.error("    • Application Access Policy not yet created (IT step)")
        log.error("    • SHARED_MAILBOX_UPN is wrong")
        log.error("    • Admin consent not yet granted")
        sys.exit(1)

    # 3. Cert file check
    cert_ok = CERT_PATH is not None and CERT_PATH.exists()
    log.info("  %s  Cert PEM:  %s", "✓" if cert_ok else "✗",
             str(CERT_PATH) if CERT_PATH else "(not configured)")

    log.info("\n%s\n  Auth check PASSED — run `python v4/main.py --once` to start.\n%s",
             _SEP2, _SEP2)


def cmd_once() -> None:
    """Run one poll cycle and exit."""
    from v4.core.config import startup_check
    startup_check("full")

    from v4.core.intake import init_db
    from v4.pipeline import run_pipeline

    init_db()
    _banner("v4 pipeline — single poll cycle")
    summary = run_pipeline()
    _log_summary(summary)


def cmd_schedule() -> None:
    """Run on a repeating schedule using APScheduler (blocking)."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError:
        log.error(
            "APScheduler not installed.\n"
            "  Fix:  pip install apscheduler\n"
            "        (or:  pip install -r v4/requirements_v4.txt)"
        )
        sys.exit(1)

    from v4.core.config import startup_check, POLL_INTERVAL_MINUTES
    startup_check("full")

    from v4.core.intake import init_db
    from v4.pipeline import run_pipeline

    init_db()

    _banner(f"v4 pipeline — scheduled mode  (every {POLL_INTERVAL_MINUTES} min)")
    log.info("  Ctrl+C to stop.  First cycle starting now…\n")

    def _cycle() -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        log.info("── Poll cycle @ %s ──────────────────────────────", ts)
        try:
            summary = run_pipeline()
            _log_summary(summary)
        except Exception:
            log.exception("Unhandled error in poll cycle — will retry next interval")

    scheduler = BlockingScheduler()
    scheduler.add_job(_cycle, "interval", minutes=POLL_INTERVAL_MINUTES, id="poll")
    try:
        _cycle()         # run immediately on startup
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("\n  Scheduler stopped.")


def cmd_review() -> None:
    """Launch the v3 interactive staging review CLI."""
    import runpy
    review_path = _V3_ROOT / "core" / "invoices" / "manual_review.py"
    if not review_path.exists():
        log.error("v3 manual_review.py not found at %s", review_path)
        sys.exit(1)
    log.info("Launching v3 review CLI…")
    runpy.run_path(str(review_path), run_name="__main__")


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _log_summary(s: dict) -> None:
    if s.get("first_run") and s.get("attachments_found", 0) == 0:
        return   # first-run snapshot already printed its own message
    parts = [
        f"new={s.get('new_messages', 0)}",
        f"processed={s.get('processed', 0)}",
        f"failed={s.get('failed', 0)}",
    ]
    if s.get("skipped_blocked", 0):
        parts.append(f"blocked={s['skipped_blocked']}  ← check logs to add senders")
    if s.get("skipped_duplicate", 0):
        parts.append(f"duplicate={s['skipped_duplicate']}")
    log.info("  Summary: %s", "  |  ".join(parts))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="v4 invoice pipeline — app-only shared mailbox intake",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "First-time setup:\n"
            "  1. Copy v4/.env.example → v4/.env and fill in all values.\n"
            "  2. python v4/main.py --check-auth\n"
            "  3. python v4/main.py --once       (first poll — snapshots the inbox)\n"
            "  4. python v4/main.py --schedule   (daemon mode)\n"
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check-auth", action="store_true",
        help="Verify app-only Graph auth and print a health summary",
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
        cmd_once()


if __name__ == "__main__":
    main()
