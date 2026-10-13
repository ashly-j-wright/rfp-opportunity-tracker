from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

TODAY = date.today()
USER_AGENT = "RFPOpportunityTracker/2.0"

@dataclass
class Opportunity:
    scraped_at: str
    source: str
    state: str
    portal: str
    title: str = ""
    agency: str = ""
    solicitation_id: str = ""
    status: str = ""
    published_date: str = ""
    due_date: str = ""
    due_time: str = ""
    buyer_contact: str = ""
    buyer_email: str = ""
    description: str = ""
    detail_url: str = ""
    matched_terms: str = ""
    fit_score: int = 0
    recommendation: str = ""
    notes: str = ""


def load_config(path: str = "config.json") -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def clean_lines(text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return [line for line in lines if line]


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_date_safe(value: str) -> date | None:
    if not value:
        return None
    try:
        return date_parser.parse(value, fuzzy=True).date()
    except Exception:
        return None


def is_future_or_current_due(value: str) -> bool:
    parsed = parse_date_safe(value)
    if parsed is None:
        return True
    return parsed >= TODAY


def session_get(url: str, delay: float = 0.0) -> requests.Response:
    if delay:
        time.sleep(delay)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain,*/*"}
    response = requests.get(url, headers=headers, timeout=45)
    response.raise_for_status()
    return response


def score_record(record: Opportunity, config: dict[str, Any]) -> Opportunity:
    text = " ".join([
        record.title or "",
        record.agency or "",
        record.solicitation_id or "",
        record.status or "",
        record.description or "",
        record.notes or ""
    ]).lower()
    matched = []
    score = 0
    weights = {k.lower(): v for k, v in config.get("keyword_weights", {}).items()}
    for term in config.get("keywords", []):
        term_l = term.lower()
        if term_l in text:
            matched.append(term)
            score += int(weights.get(term_l, 2))
    for term in config.get("negative_terms", []):
        if term.lower() in text:
            score -= 1
    record.fit_score = max(score, 0)
    record.matched_terms = ", ".join(sorted(set(matched), key=str.lower))
    if record.fit_score >= 8:
        record.recommendation = "High fit - review immediately"
    elif record.fit_score >= 4:
        record.recommendation = "Medium fit - review"
    elif record.fit_score >= 1:
        record.recommendation = "Possible fit - quick screen"
    else:
        record.recommendation = "No keyword match"
    return record


def make_opportunity(source: str, state: str, portal: str, **kwargs: Any) -> Opportunity:
    return Opportunity(
        scraped_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        source=source,
        state=state,
        portal=portal,
        **kwargs,
    )


def next_value(lines: list[str], index: int) -> str:
    raw = lines[index]
    after_colon = raw.split(":", 1)[1].strip() if ":" in raw else ""
    if after_colon:
        return after_colon
    for j in range(index + 1, min(index + 5, len(lines))):
        if lines[j] and not lines[j].endswith(":"):
            return lines[j]
    return ""


def field_after_label(text: str, label_patterns: list[str], stop_patterns: list[str] | None = None, max_chars: int = 500) -> str:
    if not text:
        return ""
    cleaned = compact_text(text)
    stops = stop_patterns or [
        "Bid Number", "Description", "Bid Opening Date", "Purchaser", "Organization", "Department", "Location",
        "Fiscal Year", "Type Code", "Allow Electronic Quote", "Alternate Id", "Required Date", "Available Date",
        "Info Contact", "Bid Type", "Purchase Method", "Begin Date", "End Date", "Pre Bid Conference",
        "Bulletin Desc", "Ship-to Address", "File Attachments", "Form Attachments", "Item Information",
        "Solicitation Start Date", "Solicitation Due Date", "Solicitation Due Time", "Solicitation Opening Date",
        "Solicitation Opening Time", "Opening Location", "No. of Addendums", "Amended Date", "Related Solicitation Files",
        "Agency", "County", "Status", "Contact Person", "Posting Date", "Due Date", "Due Time", "Created Date",
        "Last Updated", "Solicitation ID"
    ]
    for label in label_patterns:
        # Accept either "Label:" or "Label" followed by value.
        label_re = re.escape(label).replace("\\ ", r"\s+")
        pattern = rf"{label_re}\s*:?\s*(.+?)"
        for stop in stops:
            if stop.lower() == label.lower():
                continue
            stop_re = re.escape(stop).replace("\\ ", r"\s+")
            pattern += rf"(?=\s+{stop_re}\s*:|$)"
            break
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip(" :-")[:max_chars]
            return value
    return ""


def extract_regex(text: str, patterns: list[str], group: int = 1) -> str:
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return compact_text(m.group(group))
    return ""


def find_link_by_text(soup: BeautifulSoup, title: str, base_url: str) -> str:
    title_l = compact_text(title).lower()
    if not title_l:
        return base_url
    for a in soup.find_all("a", href=True):
        txt = compact_text(a.get_text(" ")).lower()
        if txt and (txt == title_l or title_l in txt or txt in title_l):
            return urljoin(base_url, a["href"])
    return base_url


def get_playwright_html(url: str, wait_ms: int = 5000, source: str = "BROWSER", state: str = "", errors: list[dict[str, str]] | None = None) -> tuple[str, str]:
    """Return (html, visible_text) using Playwright. Adds useful error messages if Chromium is not installed."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except Exception as exc:
        if errors is not None:
            errors.append({"source": source, "state": state, "portal": url, "error": f"Playwright unavailable. Run: python3 -m pip install playwright && python3 -m playwright install chromium. Detail: {exc}"})
        return "", ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1440, "height": 1100})
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
            except PlaywrightTimeoutError:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Some procurement portals show beta/browser prompts. Clicking harmless continuations improves extraction.
            for label in ["Do It Later", "Continue", "Public Access", "Open Bids", "View Open Bids", "Search"]:
                try:
                    loc = page.get_by_text(label, exact=False)
                    if loc.count() > 0:
                        loc.first.click(timeout=1500)
                        page.wait_for_timeout(1000)
                except Exception:
                    pass
            page.wait_for_timeout(wait_ms)
            html = page.content()
            try:
                text = page.inner_text("body", timeout=20000)
            except Exception:
                text = BeautifulSoup(html, "lxml").get_text("\n")
            browser.close()
            return html, text
    except Exception as exc:
        if errors is not None:
            errors.append({"source": source, "state": state, "portal": url, "error": f"Playwright render failed: {repr(exc)}. If browser missing, run: python3 -m playwright install chromium"})
        return "", ""


# ----------------------------- Existing direct connectors -----------------------------

def parse_scbo_record(lines: list[str], start: int, end: int) -> dict[str, str]:
    labels = {
        "Ad Title:": "title",
        "Purchasing Agent/Entity:": "agency",
        "Ad Publish Date:": "published_date",
        "Solicitation #:": "solicitation_id",
        "Direct Inquiries To:": "buyer_contact",
        "Bid/Submittal Due Date:": "due_date",
        "Buyer Phone#:": "buyer_phone",
        "Buyer Email:": "buyer_email",
        "Description:": "description",
        "Pre-Bid Information:": "pre_bid",
        "Full Details / Download:": "detail_text",
    }
    values: dict[str, str] = {}
    i = start
    while i < end:
        line = lines[i]
        if line in labels:
            key = labels[line]
            collected = []
            j = i + 1
            while j < end and lines[j] not in labels and lines[j] != "Ad Title:" and not lines[j].startswith("Print Ad"):
                collected.append(lines[j])
                j += 1
            values[key] = " ".join(collected).strip()
            i = j
        else:
            i += 1
    return values


def scrape_scbo(config: dict[str, Any], errors: list[dict[str, str]]) -> list[Opportunity]:
    records: list[Opportunity] = []
    today_key = TODAY.strftime("%Y-%m-%d")
    delay = float(config.get("request_delay_seconds", 1.0))
    for category_id, category_name in config.get("scbo_categories", {}).items():
        url = f"https://scbo.sc.gov/online-edition?c={category_id}-{today_key}"
        try:
            response = session_get(url, delay=delay)
            soup = BeautifulSoup(response.text, "lxml")
            lines = clean_lines(soup.get_text("\n"))
            start_indexes = [i for i, line in enumerate(lines) if line == "Ad Title:"]
            for idx, start in enumerate(start_indexes):
                end = start_indexes[idx + 1] if idx + 1 < len(start_indexes) else len(lines)
                parsed = parse_scbo_record(lines, start, end)
                if not parsed.get("title"):
                    continue
                due = parsed.get("due_date", "")
                if not is_future_or_current_due(due):
                    continue
                detail = parsed.get("detail_text", "")
                detail_url = url if not detail.startswith("http") else detail
                opp = make_opportunity(
                    source="SC_SCBO",
                    state="SC",
                    portal=url,
                    title=parsed.get("title", ""),
                    agency=parsed.get("agency", ""),
                    solicitation_id=parsed.get("solicitation_id", ""),
                    status="Open/current on SCBO listing",
                    published_date=parsed.get("published_date", ""),
                    due_date=due,
                    buyer_contact=parsed.get("buyer_contact", ""),
                    buyer_email=parsed.get("buyer_email", ""),
                    description=parsed.get("description", ""),
                    detail_url=detail_url,
                    notes=f"SCBO category: {category_name}",
                )
                records.append(score_record(opp, config))
        except Exception as exc:
            errors.append({"source": "SC_SCBO", "state": "SC", "portal": url, "error": repr(exc)})
    return records


def scrape_tx_esbd(config: dict[str, Any], errors: list[dict[str, str]]) -> list[Opportunity]:
    records: list[Opportunity] = []
    max_pages = int(config.get("max_pages_per_source", 5))
    delay = float(config.get("request_delay_seconds", 1.0))
    base = "https://www.txsmartbuy.gov/esbd"
    labels = [
        "Solicitation ID:", "Due Date:", "Due Time:", "Agency/Texas SmartBuy Member Number:",
        "Status:", "Posting Date:", "Created Date:", "Last Updated:"
    ]
    non_title_prefixes = tuple(x.replace(":", "") for x in labels)
    for page in range(1, max_pages + 1):
        url = base if page == 1 else f"{base}?page={page}"
        try:
            response = session_get(url, delay=delay)
            soup = BeautifulSoup(response.text, "lxml")
            lines = clean_lines(soup.get_text("\n"))
            for i, line in enumerate(lines):
                if not line.startswith("Solicitation ID:"):
                    continue
                title = ""
                for back in range(i - 1, max(-1, i - 8), -1):
                    candidate = lines[back].strip(" |")
                    if not candidate or candidate.startswith(non_title_prefixes) or candidate.isdigit() or candidate == "...":
                        continue
                    if candidate in ["ESBD Solicitations", "Search", "Clear Filters", "Export to CSV"]:
                        continue
                    title = candidate
                    break
                sol_id = next_value(lines, i)
                field_map = {"solicitation_id": sol_id}
                j = i + 1
                while j < min(i + 30, len(lines)):
                    current = lines[j]
                    for label in labels[1:]:
                        if current.startswith(label):
                            key = label.lower().replace("/", "_").replace(" ", "_").replace(":", "")
                            field_map[key] = next_value(lines, j)
                    if j > i + 3 and lines[j].startswith("Solicitation ID:"):
                        break
                    j += 1
                due_date = field_map.get("due_date", "")
                if not is_future_or_current_due(due_date):
                    continue
                status = field_map.get("status", "")
                if status and status.lower() in {"closed", "awarded", "no award", "posting cancelled"}:
                    continue
                detail_url = find_link_by_text(soup, title, base)
                opp = make_opportunity(
                    source="TX_ESBD",
                    state="TX",
                    portal=url,
                    title=title,
                    agency=field_map.get("agency_texas_smartbuy_member_number", ""),
                    solicitation_id=sol_id,
                    status=status,
                    published_date=field_map.get("posting_date", ""),
                    due_date=due_date,
                    due_time=field_map.get("due_time", ""),
                    description=title,
                    detail_url=detail_url,
                    notes=f"Parsed from TX ESBD page {page}",
                )
                records.append(score_record(opp, config))
        except Exception as exc:
            errors.append({"source": "TX_ESBD", "state": "TX", "portal": url, "error": repr(exc)})
    return records


# ----------------------------- New Level 3 structured connectors -----------------------------

def parse_detail_page_text(source: str, state: str, portal: str, detail_url: str, text: str, config: dict[str, Any], defaults: dict[str, str] | None = None) -> Opportunity | None:
    defaults = defaults or {}
    t = compact_text(text)
    if not t or len(t) < 40:
        return None
    title = defaults.get("title") or field_after_label(t, ["Description", "Solicitation Title", "Bid Title", "Short Description", "Project Title", "Title"], max_chars=250)
    sol_id = defaults.get("solicitation_id") or field_after_label(t, ["Bid Number", "Bid Solicitation", "Solicitation #", "Solicitation Number", "Solicitation ID", "Document/Bid Number", "Advertisement Number"], max_chars=120)
    due_date = defaults.get("due_date") or field_after_label(t, ["Bid Opening Date", "Solicitation Due Date", "Due Date", "End Date", "Close Date", "Closing Date", "Response Due Date"], max_chars=120)
    due_time = defaults.get("due_time") or field_after_label(t, ["Solicitation Due Time", "Due Time", "Bid Opening Time", "Close Time", "Closing Time"], max_chars=80)
    agency = defaults.get("agency") or field_after_label(t, ["Agency", "Organization", "Department", "Purchasing Agency", "Issuing Agency"], max_chars=180)
    buyer = defaults.get("buyer_contact") or field_after_label(t, ["Purchaser", "Buyer", "Contact Person", "Contact", "Procurement Officer"], max_chars=180)
    status = defaults.get("status") or field_after_label(t, ["Status", "Bid Type"], max_chars=80)
    published = defaults.get("published_date") or field_after_label(t, ["Available Date", "Posting Date", "Published Date", "Solicitation Start Date", "Begin Date", "Amended Date"], max_chars=120)
    description = defaults.get("description") or title or t[:1200]
    if not title:
        lines = clean_lines(text)
        for line in lines[:12]:
            if len(line) > 10 and not any(skip in line.lower() for skip in ["skip to", "login", "copyright", "image", "periscope"]):
                title = line[:250]
                break
    if due_date and not is_future_or_current_due(due_date):
        return None
    if status and status.lower() in {"closed", "awarded", "cancelled", "canceled", "inactive"}:
        return None
    opp = make_opportunity(
        source=source,
        state=state,
        portal=portal,
        title=title,
        agency=agency,
        solicitation_id=sol_id,
        status=status or "Open/current - parsed from public portal",
        published_date=published,
        due_date=due_date,
        due_time=due_time,
        buyer_contact=buyer,
        description=description,
        detail_url=detail_url,
        notes="Structured connector parsed public listing/detail page. Verify official portal before bid/no-bid decision.",
    )
    return score_record(opp, config)


def collect_links_from_html(html: str, base_url: str, include_patterns: list[str], limit: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html or "", "lxml")
    links: list[tuple[str, str]] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a.get("href", ""))
        text = compact_text(a.get_text(" "))
        if not href or href in seen:
            continue
        if any(re.search(pat, href, re.IGNORECASE) for pat in include_patterns):
            seen.add(href)
            links.append((text, href))
            if len(links) >= limit:
                break
    return links


def scrape_pa_emarketplace(config: dict[str, Any], errors: list[dict[str, str]]) -> list[Opportunity]:
    records: list[Opportunity] = []
    base = "https://www.emarketplace.state.pa.us/search.aspx"
    max_records = int(config.get("max_records_per_state", 60))
    delay = float(config.get("request_delay_seconds", 1.0))
    try:
        response = session_get(base, delay=delay)
        html = response.text
        soup = BeautifulSoup(html, "lxml")
        links = collect_links_from_html(html, base, [r"Solicitations\.aspx\?SID="], max_records)
        # Search page has a useful current-solicitations table. If detail pages are accessible, parse each detail page.
        for link_text, href in links:
            try:
                detail = session_get(href, delay=delay)
                opp = parse_detail_page_text("PA_eMarketplace", "PA", base, href, detail.text, config, defaults={"solicitation_id": link_text})
                if opp:
                    records.append(opp)
            except Exception as detail_exc:
                # Fallback to surrounding search page text for this solicitation.
                sid = link_text or extract_regex(href, [r"SID=([^&]+)"])
                page_text = compact_text(soup.get_text(" "))
                idx = page_text.find(sid)
                snippet = page_text[max(0, idx): idx + 1500] if idx >= 0 else sid
                opp = parse_detail_page_text("PA_eMarketplace", "PA", base, href, snippet, config, defaults={"solicitation_id": sid, "description": snippet})
                if opp:
                    opp.notes += f" Detail fetch failed: {repr(detail_exc)}"
                    records.append(opp)
        if not records:
            errors.append({"source": "PA_eMarketplace", "state": "PA", "portal": base, "error": "No PA solicitation records parsed from search.aspx. Portal structure may have changed."})
    except Exception as exc:
        errors.append({"source": "PA_eMarketplace", "state": "PA", "portal": base, "error": repr(exc)})
    return records


def scrape_fl_vbs(config: dict[str, Any], errors: list[dict[str, str]]) -> list[Opportunity]:
    records: list[Opportunity] = []
    base = "https://vendor.myfloridamarketplace.com/search/bids"
    max_records = int(config.get("max_records_per_state", 60))
    wait_ms = int(float(config.get("browser_wait_seconds", 8)) * 1000)
    html = ""
    text = ""
    try:
        response = session_get(base, delay=float(config.get("request_delay_seconds", 1.0)))
        html = response.text
        text = response.text
    except Exception:
        pass
    links = collect_links_from_html(html, base, [r"/search/bids/detail/\d+"], max_records)
    if not links:
        html, text = get_playwright_html(base, wait_ms=wait_ms, source="FL_VBS", state="FL", errors=errors)
        links = collect_links_from_html(html, base, [r"/search/bids/detail/\d+"], max_records)
    if not links:
        # Fallback: the rendered page may have full listings as text without detail anchors.
        if text:
            opp = parse_detail_page_text("FL_VBS", "FL", base, base, text, config, defaults={"title": "Florida VBS rendered listing - manual review required", "description": text[:1200]})
            if opp and opp.fit_score > 0:
                records.append(opp)
        if not records:
            errors.append({"source": "FL_VBS", "state": "FL", "portal": base, "error": "No Florida VBS detail links parsed. If first run, install Chromium with: python3 -m playwright install chromium"})
        return records
    for link_text, href in links[:max_records]:
        try:
            detail = session_get(href, delay=float(config.get("request_delay_seconds", 1.0)))
            defaults = {"title": link_text} if link_text else {}
            opp = parse_detail_page_text("FL_VBS", "FL", base, href, detail.text, config, defaults=defaults)
            if opp:
                records.append(opp)
        except Exception as exc:
            errors.append({"source": "FL_VBS", "state": "FL", "portal": href, "error": repr(exc)})
    return records


def scrape_njstart(config: dict[str, Any], errors: list[dict[str, str]]) -> list[Opportunity]:
    records: list[Opportunity] = []
    base = "https://www.njstart.gov/bso/view/search/external/advancedSearchBid.xhtml?openBids=true"
    max_records = int(config.get("max_records_per_state", 60))
    wait_ms = int(float(config.get("browser_wait_seconds", 8)) * 1000)
    html = ""
    text = ""
    try:
        response = session_get(base, delay=float(config.get("request_delay_seconds", 1.0)))
        html = response.text
        text = response.text
    except Exception:
        pass
    links = collect_links_from_html(html, base, [r"/bso/external/bidDetail\.sdo", r"/bso/external/bidDetail\.sda"], max_records)
    if not links:
        html, text = get_playwright_html(base, wait_ms=wait_ms, source="NJSTART", state="NJ", errors=errors)
        links = collect_links_from_html(html, base, [r"/bso/external/bidDetail\.sdo", r"/bso/external/bidDetail\.sda"], max_records)
    # Sometimes NJSTART detail links are present as JavaScript onclicks or docId values rather than hrefs.
    if not links:
        doc_ids = re.findall(r"docId[=:'\"]+([A-Z0-9\-]+)", html + "\n" + text, flags=re.IGNORECASE)
        for doc_id in list(dict.fromkeys(doc_ids))[:max_records]:
            links.append((doc_id, f"https://www.njstart.gov/bso/external/bidDetail.sdo?docId={doc_id}"))
    if not links:
        errors.append({"source": "NJSTART", "state": "NJ", "portal": base, "error": "No NJSTART open-bid detail links parsed. If first run, install Chromium with: python3 -m playwright install chromium"})
        return records
    for link_text, href in links[:max_records]:
        try:
            detail = session_get(href, delay=float(config.get("request_delay_seconds", 1.0)))
            opp = parse_detail_page_text("NJSTART", "NJ", base, href, detail.text, config, defaults={"solicitation_id": link_text})
            if opp:
                records.append(opp)
        except Exception as exc:
            errors.append({"source": "NJSTART", "state": "NJ", "portal": href, "error": repr(exc)})
    return records


def scrape_browser_structured_source(source: str, state: str, base_url: str, detail_patterns: list[str], config: dict[str, Any], errors: list[dict[str, str]], notes: str = "") -> list[Opportunity]:
    records: list[Opportunity] = []
    max_records = int(config.get("max_records_per_state", 60))
    wait_ms = int(float(config.get("browser_wait_seconds", 8)) * 1000)
    html, text = get_playwright_html(base_url, wait_ms=wait_ms, source=source, state=state, errors=errors)
    if not html and not text:
        return records
    links = collect_links_from_html(html, base_url, detail_patterns, max_records)
    if links:
        for link_text, href in links[:max_records]:
            # Use requests first, then rendered detail if needed.
            detail_text = ""
            try:
                detail_resp = session_get(href, delay=float(config.get("request_delay_seconds", 1.0)))
                detail_text = detail_resp.text
            except Exception:
                detail_html, detail_visible = get_playwright_html(href, wait_ms=wait_ms, source=source, state=state, errors=errors)
                detail_text = detail_visible or BeautifulSoup(detail_html, "lxml").get_text("\n")
            opp = parse_detail_page_text(source, state, base_url, href, detail_text, config, defaults={"title": link_text, "notes": notes})
            if opp:
                if notes:
                    opp.notes = notes + " " + opp.notes
                records.append(opp)
    else:
        # Text-table fallback: split rendered portal body into chunks around common solicitation markers.
        lines = clean_lines(text)
        chunks: list[str] = []
        current: list[str] = []
        markers = ["solicitation", "bid opening", "due date", "rfp", "ifb", "request for proposal", "opportunity"]
        for line in lines:
            if len(current) > 20 or (current and any(m in line.lower() for m in ["bid number", "solicitation #", "solicitation number", "document/bid number"])):
                chunks.append(" ".join(current))
                current = []
            if any(m in line.lower() for m in markers) or current:
                current.append(line)
        if current:
            chunks.append(" ".join(current))
        for chunk in chunks[:max_records]:
            opp = parse_detail_page_text(source, state, base_url, base_url, chunk, config, defaults={"description": chunk})
            if opp and (opp.title or opp.description) and (opp.fit_score > 0 or bool(config.get("include_all_current_records", False))):
                if notes:
                    opp.notes = notes + " " + opp.notes
                records.append(opp)
    if not records:
        errors.append({"source": source, "state": state, "portal": base_url, "error": "No structured records parsed from rendered portal. Install Chromium if needed, or portal layout may have changed."})
    return records


def scrape_md_emma(config: dict[str, Any], errors: list[dict[str, str]]) -> list[Opportunity]:
    return scrape_browser_structured_source(
        source="MD_eMMA",
        state="MD",
        base_url="https://emma.maryland.gov/page.aspx/en/rfp/request_browse_public",
        detail_patterns=[r"/page\.aspx/en/rfp/request_browse", r"/page\.aspx/en/rfp/request_manage", r"/page\.aspx/en/bpm/process_manage"],
        config=config,
        errors=errors,
        notes="Maryland eMMA public solicitations structured browser connector.",
    )


def scrape_ohio_buys(config: dict[str, Any], errors: list[dict[str, str]]) -> list[Opportunity]:
    # OhioBuys is a public portal, but the exact public route can vary. Try primary public solicitation route first, then state procurement page fallback.
    all_records: list[Opportunity] = []
    urls = [
        "https://ohiobuys.ohio.gov/page.aspx/en/rfp/request_browse_public",
        "https://procure.ohio.gov/",
    ]
    for url in urls:
        pulled = scrape_browser_structured_source(
            source="OH_OhioBuys",
            state="OH",
            base_url=url,
            detail_patterns=[r"/page\.aspx/en/rfp/request", r"/proc/", r"document", r"bid"],
            config=config,
            errors=errors,
            notes="OhioBuys/procure.ohio.gov structured browser connector.",
        )
        all_records.extend(pulled)
        if pulled:
            break
    return all_records


def scrape_colorado_vss(config: dict[str, Any], errors: list[dict[str, str]]) -> list[Opportunity]:
    return scrape_browser_structured_source(
        source="CO_VSS",
        state="CO",
        base_url="https://prd.co.cgiadvantage.com/PRDVSS1X1/Advantage4",
        detail_patterns=[r"Advantage4", r"Solicitation", r"Bid", r"Document"],
        config=config,
        errors=errors,
        notes="ColoradoVSS structured browser connector. Uses public VSS access where available.",
    )


# ----------------------------- Optional legacy browser probe -----------------------------

def browser_probe_sources(config: dict[str, Any], errors: list[dict[str, str]]) -> list[Opportunity]:
    if not config.get("enable_browser_probe_sources", False):
        return []
    hits: list[Opportunity] = []
    wait_ms = int(float(config.get("browser_probe_wait_seconds", 6)) * 1000)
    keywords = [k.lower() for k in config.get("keywords", [])]
    for src in config.get("browser_probe_sources", []):
        url = src["portal"]
        html, text = get_playwright_html(url, wait_ms=wait_ms, source=src.get("source", "BROWSER_PROBE"), state=src.get("state", ""), errors=errors)
        if not text:
            continue
        lower = text.lower()
        matched = [k for k in keywords if k in lower]
        if matched:
            snippet = ""
            first = lower.find(matched[0])
            if first >= 0:
                snippet = compact_text(text[max(0, first-300):first+500])
            opp = make_opportunity(
                source=src["source"],
                state=src["state"],
                portal=url,
                title="Keyword found on live portal page - manual review required",
                status="Browser probe match",
                description=snippet,
                detail_url=url,
                notes=src.get("notes", ""),
            )
            hits.append(score_record(opp, config))
    return hits


def dedupe(records: Iterable[Opportunity]) -> list[Opportunity]:
    seen = set()
    output = []
    for rec in records:
        key = (rec.source, rec.state, rec.solicitation_id.strip().lower(), rec.title.strip().lower(), rec.due_date, rec.detail_url)
        if key in seen:
            continue
        seen.add(key)
        output.append(rec)
    return output



def sanitize_excel_value(value: Any) -> Any:
    """Prevent Excel repair warnings by forcing formula-like text to remain text."""
    if value is None:
        return value
    if isinstance(value, str):
        value = re.sub(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]", " ", value)
        value = value.replace("\r", " ").replace("\n", " ")
        value = re.sub(r"\s+", " ", value).strip()
        if value.startswith(("=", "+", "-", "@")):
            value = "'" + value
        if len(value) > 32000:
            value = value[:32000]
    return value


def sanitize_dataframe_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    cleaned = df.copy()
    for column in cleaned.columns:
        cleaned[column] = cleaned[column].map(sanitize_excel_value)
    return cleaned

def write_outputs(records: list[Opportunity], all_current: list[Opportunity], errors: list[dict[str, str]], config: dict[str, Any]) -> None:
    output_excel = Path(config.get("output_excel", "output/rfp_results.xlsx"))
    output_csv = Path(config.get("output_csv", "output/rfp_results.csv"))
    output_excel.parent.mkdir(parents=True, exist_ok=True)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(r) for r in records]
    all_rows = [asdict(r) for r in all_current]
    error_rows = errors or []
    # Keep the Excel/CSV output simple for daily opportunity review.
    # The scraper still uses internal fields such as status, due_date, matched_terms,
    # and fit_score to screen results, but only the columns below are written out.
    cols = [
        "scraped_at",
        "source",
        "state",
        "title",
        "agency",
        "solicitation_id",
        "description",
        "detail_url",
        "portal",
    ]
    df = pd.DataFrame(rows, columns=cols)
    all_df = pd.DataFrame(all_rows, columns=cols)
    err_df = pd.DataFrame(error_rows, columns=["source", "state", "portal", "error"])
    connector_list = ", ".join([c.get("name", "") for c in config.get("enabled_connectors", []) if c.get("enabled", True)])
    summary = pd.DataFrame([
        {"Metric": "Run date", "Value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        {"Metric": "Qualified matches", "Value": len(df)},
        {"Metric": "All current records pulled", "Value": len(all_df)},
        {"Metric": "Errors", "Value": len(err_df)},
        {"Metric": "Minimum fit score", "Value": config.get("min_fit_score")},
        {"Metric": "Enabled connectors", "Value": connector_list},
        {"Metric": "Browser probe enabled", "Value": config.get("enable_browser_probe_sources")},
    ])
    # V4.2.1: output columns are intentionally simplified, so sort only by columns
    # that are still present in the Excel workbook.
    sort_cols = [c for c in ["state", "source", "title"] if c in df.columns]
    all_sort_cols = [c for c in ["state", "source", "title"] if c in all_df.columns]
    if not df.empty and sort_cols:
        df = df.sort_values(by=sort_cols, ascending=[True] * len(sort_cols))
    if not all_df.empty and all_sort_cols:
        all_df = all_df.sort_values(by=all_sort_cols, ascending=[True] * len(all_sort_cols))
    df.to_csv(output_csv, index=False)

    # Excel-safe copies prevent formula-repair warnings caused by portal text
    # that begins with =, +, -, or @, and remove invalid XML control characters.
    summary_xlsx = sanitize_dataframe_for_excel(summary)
    df_xlsx = sanitize_dataframe_for_excel(df)
    all_df_xlsx = sanitize_dataframe_for_excel(all_df)
    err_df_xlsx = sanitize_dataframe_for_excel(err_df)

    with pd.ExcelWriter(output_excel, engine="openpyxl") as writer:
        summary_xlsx.to_excel(writer, sheet_name="Run Summary", index=False)
        df_xlsx.to_excel(writer, sheet_name="Qualified Matches", index=False)
        all_df_xlsx.to_excel(writer, sheet_name="All Current Pulled", index=False)
        err_df_xlsx.to_excel(writer, sheet_name="Errors", index=False)
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                max_len = 0
                column = col[0].column_letter
                for cell in col[:200]:
                    val = "" if cell.value is None else str(cell.value)
                    max_len = max(max_len, min(len(val), 70))
                ws.column_dimensions[column].width = max(12, min(max_len + 2, 52))
    print(f"\nDone. Qualified matches: {len(df)}")
    print(f"Excel output: {output_excel.resolve()}")
    print(f"CSV output: {output_csv.resolve()}")
    if errors:
        print(f"Warnings/errors captured: {len(errors)}. See the Errors sheet.")


def main() -> None:
    config = load_config()
    errors: list[dict[str, str]] = []
    all_current: list[Opportunity] = []
    connector_map = {
        "SC_SCBO": scrape_scbo,
        "TX_ESBD": scrape_tx_esbd,
        "CO_VSS": scrape_colorado_vss,
        "MD_eMMA": scrape_md_emma,
        "OH_OhioBuys": scrape_ohio_buys,
        "FL_VBS": scrape_fl_vbs,
        "NJSTART": scrape_njstart,
        "PA_eMarketplace": scrape_pa_emarketplace,
        "BROWSER_PROBE": browser_probe_sources,
    }
    enabled = config.get("enabled_connectors") or [
        {"name": "SC_SCBO", "enabled": True},
        {"name": "TX_ESBD", "enabled": True},
        {"name": "CO_VSS", "enabled": True},
        {"name": "MD_eMMA", "enabled": True},
        {"name": "OH_OhioBuys", "enabled": True},
        {"name": "FL_VBS", "enabled": True},
        {"name": "NJSTART", "enabled": True},
        {"name": "PA_eMarketplace", "enabled": True},
    ]
    for item in enabled:
        name = item.get("name")
        if not item.get("enabled", True):
            continue
        connector = connector_map.get(name)
        if not connector:
            errors.append({"source": str(name), "state": "", "portal": "", "error": "Connector not found in connector_map."})
            continue
        try:
            pulled = connector(config, errors)
            all_current.extend(pulled)
        except Exception as exc:
            errors.append({"source": str(name), "state": "", "portal": "", "error": f"Connector crashed: {repr(exc)}"})
    all_current = dedupe(all_current)
    min_score = int(config.get("min_fit_score", 2))
    include_all = bool(config.get("include_all_current_records", False))
    if include_all:
        qualified = all_current
    else:
        qualified = [r for r in all_current if r.fit_score >= min_score]
    qualified = dedupe(qualified)
    write_outputs(qualified, all_current, errors, config)


if __name__ == "__main__":
    main()
