"""
Interactive dataset inspector for the v3 invoice pipeline.

Navigation:
  Main menu  →  dataset browser  →  record detail  →  back
  F = filter  C = clear filters  N/P = next/prev page
  E = export CSV  B = back  Q = quit
"""

import csv
import json
import sqlite3
import sys
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table

console = Console()

PAGE_SIZE = 20

_V3_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ===========================================================================
# DATA LOADERS
# ===========================================================================

def load_invoices(staging_dir: Path) -> list[dict]:
    if not staging_dir.exists():
        return []
    records = []
    for f in sorted(staging_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            d["_filename"] = f.name
            d["_path"] = str(f)
            records.append(d)
        except Exception as e:
            records.append({"_filename": f.name, "_error": str(e), "_path": str(f)})
    return records


def load_db_rows(db_path: Path, table: str, order: str) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order} DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===========================================================================
# FILTER HELPERS
# ===========================================================================

def _str_contains(haystack: str | None, needle: str) -> bool:
    return needle.lower() in (haystack or "").lower()


def apply_invoice_filters(records: list[dict], f: dict) -> list[dict]:
    out = []
    for r in records:
        if "_error" in r:
            out.append(r)
            continue
        cust = r.get("customer", {})
        name  = (cust.get("name")  or {}).get("value") or ""
        email = (cust.get("email") or {}).get("value") or ""
        total = (r.get("totals") or {}).get("total")
        conf  = r.get("parse_confidence", "")

        if f.get("customer") and not (_str_contains(name, f["customer"]) or _str_contains(email, f["customer"])):
            continue
        if f.get("min_total") is not None and (total is None or total < f["min_total"]):
            continue
        if f.get("max_total") is not None and (total is None or total > f["max_total"]):
            continue
        if f.get("confidence") and conf.lower() != f["confidence"].lower():
            continue
        out.append(r)
    return out


def apply_template_filters(rows: list[dict], f: dict) -> list[dict]:
    out = []
    for r in rows:
        if f.get("name") and not (
            _str_contains(r.get("customer_name"), f["name"])
            or _str_contains(r.get("lookup_key"), f["name"])
        ):
            continue
        if f.get("min_confirmed") is not None and r.get("confirmed_count", 0) < f["min_confirmed"]:
            continue
        out.append(r)
    return out


def apply_item_filters(rows: list[dict], f: dict) -> list[dict]:
    out = []
    for r in rows:
        if f.get("sku")    and not _str_contains(r.get("sku"),         f["sku"]):
            continue
        if f.get("desc")   and not _str_contains(r.get("description"), f["desc"]):
            continue
        if f.get("source") and (r.get("source") or "").lower() != f["source"].lower():
            continue
        out.append(r)
    return out


# ===========================================================================
# TABLE RENDERERS
# ===========================================================================

_CONF_STYLE = {"high": "green", "medium": "yellow", "low": "red", "HIGH": "green", "MEDIUM": "yellow", "LOW": "red"}


def _fmt_total(v) -> str:
    return f"${v:,.2f}" if v is not None else "—"


def invoice_table(records: list[dict], page: int, status: str) -> tuple[Table, int]:
    start = page * PAGE_SIZE
    chunk = records[start : start + PAGE_SIZE]
    total_pages = max(1, (len(records) + PAGE_SIZE - 1) // PAGE_SIZE)

    t = Table(box=box.ROUNDED, header_style="bold cyan", expand=True, show_lines=False)
    t.add_column("#", style="dim", width=5, no_wrap=True)
    t.add_column("File", overflow="fold")
    t.add_column("Customer")
    t.add_column("Email")
    t.add_column("Total", justify="right", no_wrap=True)
    t.add_column("Tax",   justify="right", no_wrap=True)
    t.add_column("Items", justify="center", width=6)
    if status == "pending":
        t.add_column("Conf",  justify="center", width=7)
        t.add_column("Warns", justify="center", width=6)

    for i, r in enumerate(chunk, start + 1):
        if "_error" in r:
            t.add_row(str(i), r["_filename"], f"[red]{r['_error']}[/red]")
            continue
        cust   = r.get("customer", {})
        name   = (cust.get("name")  or {}).get("value") or "—"
        email  = (cust.get("email") or {}).get("value") or "—"
        totals = r.get("totals") or {}
        items  = str(len(r.get("line_items", [])))
        row = [str(i), r["_filename"], name, email,
               _fmt_total(totals.get("total")), _fmt_total(totals.get("tax")), items]
        if status == "pending":
            conf = r.get("parse_confidence", "?").upper()
            style = _CONF_STYLE.get(conf, "")
            warns = str(len(r.get("warnings", [])))
            row += [f"[{style}]{conf}[/{style}]" if style else conf, warns]
        t.add_row(*row)

    return t, total_pages


def templates_table(rows: list[dict], page: int) -> tuple[Table, int]:
    start = page * PAGE_SIZE
    chunk = rows[start : start + PAGE_SIZE]
    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)

    t = Table(box=box.ROUNDED, header_style="bold cyan", expand=True)
    t.add_column("#", style="dim", width=5)
    t.add_column("Lookup Key", style="bold")
    t.add_column("Name")
    t.add_column("Email")
    t.add_column("ABN")
    t.add_column("Confirmed", justify="center", width=10)
    t.add_column("Updated", width=16)

    for i, r in enumerate(chunk, start + 1):
        t.add_row(
            str(i),
            r.get("lookup_key")    or "—",
            r.get("customer_name") or "—",
            r.get("customer_email") or "—",
            r.get("customer_abn")  or "—",
            str(r.get("confirmed_count", 0)),
            (r.get("updated_at") or "")[:16],
        )
    return t, total_pages


def items_table(rows: list[dict], page: int) -> tuple[Table, int]:
    start = page * PAGE_SIZE
    chunk = rows[start : start + PAGE_SIZE]
    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)

    t = Table(box=box.ROUNDED, header_style="bold cyan", expand=True)
    t.add_column("#", style="dim", width=5)
    t.add_column("SKU", style="bold", width=12)
    t.add_column("Description")
    t.add_column("UOM", justify="center", width=6)
    t.add_column("Unit Price", justify="right", width=12)
    t.add_column("Source", justify="center", width=8)
    t.add_column("Confirmed", justify="center", width=10)

    for i, r in enumerate(chunk, start + 1):
        price  = r.get("unit_price")
        src    = r.get("source") or "—"
        src_col = "green" if src == "erp" else "blue"
        t.add_row(
            str(i),
            r.get("sku")         or "—",
            r.get("description") or "—",
            r.get("uom")         or "—",
            _fmt_total(price),
            f"[{src_col}]{src}[/{src_col}]",
            str(r.get("confirmed_count", 0)),
        )
    return t, total_pages


# ===========================================================================
# DETAIL VIEWS
# ===========================================================================

def invoice_detail(r: dict):
    while True:
        console.clear()
        cust = r.get("customer", {})
        name = (cust.get("name") or {}).get("value") or "—"
        console.print(Panel(f"[bold]{r['_filename']}[/bold]  —  {name}", style="cyan"))

        # Customer fields
        ct = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        ct.add_column("Field", style="dim", width=10)
        ct.add_column("Value")
        ct.add_column("Conf", style="dim", width=8)
        for field in ["name", "email", "phone", "abn", "address"]:
            fv  = (cust.get(field) or {})
            val = fv.get("value") or "—"
            cfn = fv.get("confidence") or "—"
            ct.add_row(field.upper(), val, cfn)
        console.print(Panel(ct, title="Customer", border_style="dim"))

        # Line items
        items = r.get("line_items", [])
        if items:
            li = Table(box=box.SIMPLE, show_header=True, header_style="bold")
            li.add_column("#",         width=4)
            li.add_column("SKU",       width=12)
            li.add_column("Cust Ref",  width=12)
            li.add_column("Description")
            li.add_column("Qty",       justify="right", width=8)
            li.add_column("UOM",       width=6)
            li.add_column("Unit $",    justify="right", width=10)
            li.add_column("Total $",   justify="right", width=10)
            li.add_column("Conf",      justify="center", width=8)
            for item in items:
                conf = item.get("confidence", "")
                style = _CONF_STYLE.get(conf, "")
                qty = item.get("quantity")
                li.add_row(
                    str(item.get("line_number", "")),
                    item.get("sku")          or "—",
                    item.get("customer_ref") or "—",
                    item.get("description")  or "—",
                    f"{qty:g}" if qty is not None else "—",
                    item.get("uom")          or "—",
                    _fmt_total(item.get("unit_price")),
                    _fmt_total(item.get("line_total")),
                    f"[{style}]{conf}[/{style}]" if style else conf,
                )
            console.print(Panel(li, title=f"Line Items ({len(items)})", border_style="dim"))

        # Totals
        totals = r.get("totals") or {}
        console.print(
            f"  Subtotal: {_fmt_total(totals.get('subtotal'))}  "
            f"Tax: {_fmt_total(totals.get('tax'))}  "
            f"[bold]Total: {_fmt_total(totals.get('total'))}[/bold]"
        )

        # Warnings
        warns = r.get("warnings", [])
        if warns:
            console.print(Panel(
                "\n".join(f"[yellow]! {w}[/yellow]" for w in warns),
                title="Warnings", border_style="yellow"
            ))

        console.print()
        console.print("[dim]  J=raw JSON  B=back[/dim]")
        action = Prompt.ask(">", default="b").strip().lower()

        if action == "j":
            console.clear()
            r_clean = {k: v for k, v in r.items() if not k.startswith("_")}
            with console.pager(styles=True):
                console.print(Syntax(json.dumps(r_clean, indent=2), "json", theme="monokai"))
        else:
            break


def generic_detail(r: dict, title: str):
    console.clear()
    console.print(Panel(f"[bold]{title}[/bold]", style="cyan"))
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t.add_column("Field", style="dim", width=22)
    t.add_column("Value")
    for k, v in r.items():
        t.add_row(str(k), str(v) if v is not None else "—")
    console.print(t)
    console.print()
    Prompt.ask("[dim]Press Enter to go back[/dim]", default="")


# ===========================================================================
# CSV EXPORT
# ===========================================================================

def export_invoices_csv(records: list[dict], status: str):
    out = Path(f"invoice_{status}_export.csv")
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["filename", "customer_name", "customer_email", "customer_abn",
                    "subtotal", "tax", "total", "line_items", "parse_confidence", "warnings"])
        for r in records:
            if "_error" in r:
                continue
            cust   = r.get("customer", {})
            totals = r.get("totals") or {}
            w.writerow([
                r.get("_filename", ""),
                (cust.get("name")  or {}).get("value") or "",
                (cust.get("email") or {}).get("value") or "",
                (cust.get("abn")   or {}).get("value") or "",
                totals.get("subtotal", ""),
                totals.get("tax", ""),
                totals.get("total", ""),
                len(r.get("line_items", [])),
                r.get("parse_confidence", ""),
                len(r.get("warnings", [])),
            ])
    console.print(f"[green]Exported {len(records)} records → {out.resolve()}[/green]")
    Prompt.ask("[dim]Press Enter to continue[/dim]", default="")


def export_templates_csv(rows: list[dict]):
    out = Path("templates_export.csv")
    if rows:
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
    console.print(f"[green]Exported {len(rows)} records → {out.resolve()}[/green]")
    Prompt.ask("[dim]Press Enter to continue[/dim]", default="")


def export_items_csv(rows: list[dict]):
    out = Path("items_export.csv")
    if rows:
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
    console.print(f"[green]Exported {len(rows)} records → {out.resolve()}[/green]")
    Prompt.ask("[dim]Press Enter to continue[/dim]", default="")


# ===========================================================================
# FILTER PROMPTS
# ===========================================================================

def _fmt_filters(f: dict, keys: list[tuple[str, str]]) -> str:
    parts = [f"  [dim]{label}[/dim]=[bold]{f[k]}[/bold]" for k, label in keys if f.get(k) is not None and f.get(k) != ""]
    return "".join(parts)


def prompt_invoice_filters(f: dict, status: str) -> dict:
    console.print()
    f["customer"] = Prompt.ask("Customer name/email (partial, blank=any)", default=f.get("customer") or "").strip()
    if status == "pending":
        f["confidence"] = Prompt.ask("Confidence (high/medium/low, blank=any)", default=f.get("confidence") or "").strip()
    mn = Prompt.ask("Min total ($, blank=none)", default="" if f.get("min_total") is None else str(f["min_total"])).strip()
    f["min_total"] = float(mn) if mn else None
    mx = Prompt.ask("Max total ($, blank=none)", default="" if f.get("max_total") is None else str(f["max_total"])).strip()
    f["max_total"] = float(mx) if mx else None
    return f


def prompt_template_filters(f: dict) -> dict:
    console.print()
    f["name"] = Prompt.ask("Name / lookup key (partial, blank=any)", default=f.get("name") or "").strip()
    mc = Prompt.ask("Min confirmed count (blank=any)", default="" if f.get("min_confirmed") is None else str(f["min_confirmed"])).strip()
    f["min_confirmed"] = int(mc) if mc else None
    return f


def prompt_item_filters(f: dict) -> dict:
    console.print()
    f["sku"]    = Prompt.ask("SKU (partial, blank=any)",         default=f.get("sku")    or "").strip()
    f["desc"]   = Prompt.ask("Description (partial, blank=any)", default=f.get("desc")   or "").strip()
    f["source"] = Prompt.ask("Source (erp/llm, blank=any)",      default=f.get("source") or "").strip()
    return f


# ===========================================================================
# BROWSERS
# ===========================================================================

def _footer(page: int, total_pages: int):
    console.print(
        f"\n[dim]  Page {page + 1}/{total_pages}  |  "
        "#=open  F=filter  C=clear  N=next  P=prev  E=export  B=back[/dim]"
    )


def _resolve_row(action: str, active: list, page: int) -> int | None:
    """Return 0-based index into active list, or None."""
    try:
        n = int(action)
        idx = n - 1
        if 0 <= idx < len(active):
            return idx
    except ValueError:
        pass
    return None


def invoice_browser(staging_dir: Path, status: str):
    records = load_invoices(staging_dir)
    filters: dict = {}
    page = 0

    while True:
        active = apply_invoice_filters(records, filters)
        total_pages = max(1, (len(active) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages - 1)

        console.clear()
        filter_str = _fmt_filters(filters, [
            ("customer", "customer"), ("confidence", "conf"),
            ("min_total", "min$"), ("max_total", "max$"),
        ])
        console.print(Panel(
            f"[bold]{status.upper()} INVOICES[/bold]  {len(active)}/{len(records)} records{filter_str}",
            style="cyan",
        ))

        if not active:
            console.print("  [dim]No records match the current filters.[/dim]")
            _footer(page, 1)
        else:
            tbl, total_pages = invoice_table(active, page, status)
            console.print(tbl)
            _footer(page, total_pages)

        action = Prompt.ask(">", default="b").strip().lower()

        if action == "b":
            break
        elif action == "f":
            filters = prompt_invoice_filters(filters, status)
            page = 0
        elif action == "c":
            filters = {}
            page = 0
        elif action == "n":
            if page < total_pages - 1:
                page += 1
        elif action == "p":
            if page > 0:
                page -= 1
        elif action == "e":
            export_invoices_csv(active, status)
        else:
            idx = _resolve_row(action, active, page)
            if idx is not None:
                invoice_detail(active[idx])


def templates_browser(db_path: Path):
    rows = load_db_rows(db_path, "customer_templates", "updated_at")
    filters: dict = {}
    page = 0

    while True:
        active = apply_template_filters(rows, filters)
        total_pages = max(1, (len(active) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages - 1)

        console.clear()
        filter_str = _fmt_filters(filters, [("name", "name"), ("min_confirmed", "confirmed>=")])
        console.print(Panel(
            f"[bold]CUSTOMER TEMPLATES[/bold]  {len(active)}/{len(rows)} records{filter_str}",
            style="cyan",
        ))

        if not active:
            console.print("  [dim]No records match.[/dim]")
            _footer(page, 1)
        else:
            tbl, total_pages = templates_table(active, page)
            console.print(tbl)
            _footer(page, total_pages)

        action = Prompt.ask(">", default="b").strip().lower()

        if action == "b":
            break
        elif action == "f":
            filters = prompt_template_filters(filters)
            page = 0
        elif action == "c":
            filters = {}
            page = 0
        elif action == "n":
            if page < total_pages - 1:
                page += 1
        elif action == "p":
            if page > 0:
                page -= 1
        elif action == "e":
            export_templates_csv(active)
        else:
            idx = _resolve_row(action, active, page)
            if idx is not None:
                generic_detail(active[idx], active[idx].get("lookup_key") or str(idx + 1))


def items_browser(db_path: Path):
    rows = load_db_rows(db_path, "item_codes", "updated_at")
    filters: dict = {}
    page = 0

    while True:
        active = apply_item_filters(rows, filters)
        total_pages = max(1, (len(active) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages - 1)

        console.clear()
        filter_str = _fmt_filters(filters, [("sku", "sku"), ("desc", "desc"), ("source", "source")])
        console.print(Panel(
            f"[bold]ITEM CODES[/bold]  {len(active)}/{len(rows)} records{filter_str}",
            style="cyan",
        ))

        if not active:
            console.print("  [dim]No records match.[/dim]")
            _footer(page, 1)
        else:
            tbl, total_pages = items_table(active, page)
            console.print(tbl)
            _footer(page, total_pages)

        action = Prompt.ask(">", default="b").strip().lower()

        if action == "b":
            break
        elif action == "f":
            filters = prompt_item_filters(filters)
            page = 0
        elif action == "c":
            filters = {}
            page = 0
        elif action == "n":
            if page < total_pages - 1:
                page += 1
        elif action == "p":
            if page > 0:
                page -= 1
        elif action == "e":
            export_items_csv(active)
        else:
            idx = _resolve_row(action, active, page)
            if idx is not None:
                generic_detail(active[idx], active[idx].get("sku") or active[idx].get("description") or str(idx + 1))


# ===========================================================================
# MAIN MENU
# ===========================================================================

def main():
    is_live = "--live" in sys.argv
    if not is_live and "--test" not in sys.argv:
        choice = Prompt.ask("Mode", choices=["live", "test"], default="test")
        is_live = choice == "live"

    mode_label = "LIVE" if is_live else "TEST SANDBOX"
    data_root  = _V3_ROOT / ("data" if is_live else "tests/data")
    db_path    = data_root / "invoice_memory.db"
    staging    = data_root / "invoice_staging"

    while True:
        console.clear()
        console.print(Panel(
            f"[bold cyan]INVOICE DATASET INSPECTOR[/bold cyan]  [dim]{mode_label}[/dim]",
            style="cyan",
        ))
        console.print("  [bold]A[/bold]  Approved invoices")
        console.print("  [bold]P[/bold]  Pending invoices")
        console.print("  [bold]T[/bold]  Customer templates")
        console.print("  [bold]I[/bold]  Item codes")
        console.print("  [bold]Q[/bold]  Quit")
        console.print()

        choice = Prompt.ask(">", choices=["a", "p", "t", "i", "q"], default="q")

        if choice == "a":
            invoice_browser(staging / "approved", "approved")
        elif choice == "p":
            invoice_browser(staging / "pending", "pending")
        elif choice == "t":
            templates_browser(db_path)
        elif choice == "i":
            items_browser(db_path)
        elif choice == "q":
            break


if __name__ == "__main__":
    main()
