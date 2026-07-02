"""
D2 Trivago — Hotel-Only Pricing × Clicks Decision Engine
Streamlit app · upload Trivago "Results ALL" comparison files + Clicks & Costs Master
Persistent SQLite DB · margin/markup logic · clicks-vs-price decision matrix
"""
import streamlit as st
import pandas as pd
import numpy as np
import sqlite3
import re
import os
import json
import hmac
from datetime import datetime, date
import plotly.graph_objects as go
import plotly.express as px

try:
    from rapidfuzz import fuzz, process
    HAS_FUZZ = True
except ImportError:
    HAS_FUZZ = False

st.set_page_config(page_title="D2 Trivago Intelligence", page_icon="🏨",
                   layout="wide", initial_sidebar_state="expanded")

# ══════════════════════════════════════════════════════════════════════════════
# ACCESS CONTROL — password stored in the app's Settings → Secrets (st.secrets)
# On Streamlit Cloud: app ⋯ menu → Settings → Secrets, add:  password = "yourpassword"
# (or a section:  [auth]\n password = "yourpassword" ).  Locally: .streamlit/secrets.toml
# ══════════════════════════════════════════════════════════════════════════════
def _configured_password():
    try:
        s = st.secrets
    except Exception:
        return None
    try:
        if "password" in s:
            return str(s["password"])
        if "auth" in s and "password" in s["auth"]:
            return str(s["auth"]["password"])
    except Exception:
        return None
    return None

def require_login():
    """Gate the app behind a password taken from st.secrets. If no password is configured,
    the app stays open (so it never hard-locks) but shows how to set one."""
    expected = _configured_password()
    if not expected:
        return True                                  # no password set → allow (see note above)
    if st.session_state.get("auth_ok"):
        return True

    def _check():
        if hmac.compare_digest(str(st.session_state.get("pw_input", "")), expected):
            st.session_state["auth_ok"] = True
            st.session_state["pw_input"] = ""        # don't retain the password
        else:
            st.session_state["auth_ok"] = False

    st.markdown("## 🔒 D2 Trivago — Hotel-Only Pricing Engine")
    st.text_input("Password", type="password", key="pw_input", on_change=_check)
    st.button("Enter", on_click=_check)
    if st.session_state.get("auth_ok") is False:
        st.error("Incorrect password — please try again.")
    st.caption("Access is restricted. The password is set by your admin in the app's "
               "Settings → Secrets.")
    return bool(st.session_state.get("auth_ok"))

if not require_login():
    st.stop()

DB_PATH = "trivago_data.db"

if os.path.exists("reset_flag.txt"):
    try:
        os.remove(DB_PATH); os.remove("reset_flag.txt")
    except Exception:
        pass

# ── Commercial thresholds ──────────────────────────────────────────────────────
MARGIN_FLOOR      = 7.0    # never price below this markup %
MARGIN_NEAR       = 8.5    # caution band (display only)
MARGIN_CEILING    = 15.0   # never price above this markup %
HOLD_BAND         = 5.0    # +/- band considered "level" on price (stance display)
FUZZ_THRESHOLD    = 78     # hotel name match score to attach clicks

# ── Markup-suggestion engine (Part 1 of the logic brief) ────────────────────────
NOISE_GAP         = 0.5    # a "loss" within this % of break-even = a draw (noise), never chased
WR_HIGH           = 70.0   # at/above this win rate, bias to RAISE (capture margin) not cut
WR_KEEP           = 70.0   # when raising, don't let win rate fall below this
FLIP_FRAC         = 0.10   # when raising, tolerate flipping at most ~10% of solid wins to losses
STEP              = 0.25   # search granularity (pp)
BAND              = 5.0    # max single-pass change (pp)

# ── Product-gap flagging (Part 2 of the logic brief) ────────────────────────────
PRODUCT_GAP_SHARE = 0.5    # >= this share of a hotel's losses below floor => product gap
PRODUCT_GAP_MIN   = 2      # need at least this many below-floor losses to flag

st.markdown("""
<style>
[data-testid="stSidebar"]{background:#0E2A47}
[data-testid="stSidebar"] *{color:#fff!important}
</style>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS comparison(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT, platform TEXT, file_date TEXT, destination TEXT,
        giata TEXT, hotel_name TEXT, board TEXT,
        check_in TEXT, dep_month TEXT, dep_window TEXT, nights INTEGER,
        supplier TEXT, position REAL, lead_competitor TEXT,
        d2_price REAL, comp_price REAL, d2_live REAL,
        result TEXT, diff_pct REAL,
        current_margin REAL, margin_after REAL, margin_headroom REAL,
        margin_flag TEXT, uploaded_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS clicks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT, platform TEXT, hotel_name TEXT, destination TEXT,
        clicks REAL, costs REAL, cpc REAL, bookings REAL,
        revenue REAL, profit REAL, spend_profit_pct REAL, uploaded_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS bookings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT, giata TEXT, hotel_name TEXT, destination TEXT,
        bookings INTEGER, revenue REAL, profit REAL, avg_margin REAL,
        dep_month TEXT, uploaded_at TEXT)""")
    # migrate legacy bookings table (old schema had bkgs_4wk/bkgs_py)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()]
    if "bookings" not in cols:
        conn.execute("DROP TABLE bookings")
        conn.execute("""CREATE TABLE bookings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT, giata TEXT, hotel_name TEXT, destination TEXT,
            bookings INTEGER, revenue REAL, profit REAL, avg_margin REAL,
            dep_month TEXT, uploaded_at TEXT)""")
    # month-level booking aggregates (by booked month AND departure/travel month)
    conn.execute("""CREATE TABLE IF NOT EXISTS booking_months(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT, giata TEXT, destination TEXT,
        booked_month TEXT, dep_month TEXT,
        bookings INTEGER, revenue REAL, profit REAL, avg_margin REAL,
        uploaded_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT, rule_id TEXT, enabled INTEGER, applies_to INTEGER,
        action_type INTEGER, markup_type INTEGER, value REAL, op INTEGER,
        markup_app INTEGER, giatas TEXT, giata_op TEXT, is_package TEXT,
        n_conditions INTEGER, date_gated INTEGER, gen_text TEXT, uploaded_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS offers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT, giata TEXT, hotel_name TEXT, destination TEXT,
        offer_type TEXT, is_exclusive INTEGER, deal_status TEXT,
        book_from TEXT, book_to TEXT, travel_from TEXT, travel_to TEXT,
        details TEXT, uploaded_at TEXT)""")
    # shared completion-tracking log — one row per hotel × pricing-data-date (upserts in place)
    conn.execute("""CREATE TABLE IF NOT EXISTS completions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        giata TEXT, hotel_name TEXT, destination TEXT, data_date TEXT,
        done INTEGER, suggested_action TEXT, note TEXT, initials TEXT,
        updated_at TEXT, UNIQUE(giata, data_date))""")
    conn.commit(); conn.close()

init_db()

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def fix_enc(s):
    """Repair latin1/utf-8 mojibake (e.g. MÃ¶venpick -> Mövenpick)."""
    if s is None:
        return ""
    s = str(s)
    if "Ã" in s or "Â" in s:
        try:
            return s.encode("latin-1").decode("utf-8")
        except Exception:
            return s
    return s

def parse_date(val):
    if val is None:
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.date() if hasattr(val, "date") else val
    s = str(val).strip()
    if not s or s in ("", "nan", "None", "NaT", "-"):
        return None
    if " 00:00:00" in s:
        s = s.replace(" 00:00:00", "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None

def dep_window(dep_dt, run_dt):
    if dep_dt is None or run_dt is None:
        return "241+"
    days = (dep_dt - run_dt).days
    if days <= 60:
        return "0-60"
    if days <= 120:
        return "61-120"
    if days <= 240:
        return "121-240"
    return "241+"

def clean_num(val):
    if val is None:
        return np.nan
    s = str(val).replace("£", "").replace(",", "").replace("%", "").replace(" ", "").strip()
    if s in ("", "-", "nan", "None", "#VALUE!", "#DIV/0!", "#REF!", "#N/A"):
        return np.nan
    try:
        return float(s)
    except Exception:
        return np.nan

def to_pct(val):
    """Source margins are decimals (0.1211). Convert to % robustly."""
    v = clean_num(val)
    if pd.isna(v):
        return np.nan
    if abs(v) <= 1.5:          # decimal form
        return round(v * 100, 2)
    return round(v, 2)          # already a percentage

def margin_flag(v):
    try:
        v = float(v)
    except Exception:
        return "unknown"
    if v <= 0:
        return "unknown"
    if v < MARGIN_FLOOR:
        return "floor"
    if v < MARGIN_NEAR:
        return "near"
    if v > MARGIN_CEILING:
        return "ceiling"
    return "ok"

def margin_cell(cur, after):
    try:
        cur = float(cur); after = float(after)
        if cur <= 0 and after <= 0:
            return "—"
        delta = after - cur
        flag = margin_flag(after)
        lbl = {"floor": "⚠FLOOR", "near": "NEAR", "ceiling": "↑HEADROOM",
               "ok": "", "unknown": ""}[flag]
        return f"{cur:.1f}% → {after:.1f}% {lbl} ({delta:+.1f}pp)"
    except Exception:
        return "—"

def extract_meta(fname):
    """Trivago_DXB_08_06_26.xlsm -> platform, destination code, run date."""
    base = re.sub(r"\.(xlsm|xlsx|xlsb|csv)$", "", fname, flags=re.I)
    parts = base.split("_")
    platform = parts[0] if parts else "Trivago"
    dest = parts[1] if len(parts) > 1 else "UNK"
    try:
        d, m, y = int(parts[-3]), int(parts[-2]), int(parts[-1])
        if y < 100:
            y += 2000
        fd = date(y, m, d)
    except Exception:
        fd = date.today()
    return platform, dest, fd

# Destination code -> readable name (extendable)
DEST_NAMES = {
    "DXB": "Dubai", "MLE": "Maldives", "MRU": "Mauritius", "TFS": "Tenerife",
    "ACE": "Lanzarote", "AUH": "Abu Dhabi", "BGI": "Barbados", "ANU": "Antigua",
}

# ══════════════════════════════════════════════════════════════════════════════
# COMPARISON PARSER  (Trivago "Results ALL")
# ══════════════════════════════════════════════════════════════════════════════
RESULTS_SHEET_CANDIDATES = ["results all", "results"]

def find_results_sheet(xl):
    low = {s.lower(): s for s in xl.sheet_names}
    for cand in RESULTS_SHEET_CANDIDATES:
        for lk, orig in low.items():
            if lk == cand:
                return orig
    for lk, orig in low.items():
        if "results all" in lk:
            return orig
    for lk, orig in low.items():
        if lk.startswith("results"):
            return orig
    return None

def parse_comparison_file(uploaded_file, fname):
    platform, dest_code, file_dt = extract_meta(fname)
    dest_name = DEST_NAMES.get(dest_code.upper(), dest_code)
    rows, dbg = [], []
    try:
        xl = pd.ExcelFile(uploaded_file, engine="openpyxl")
    except Exception as e:
        return [], [f"ERROR opening: {e}"]
    sheet = find_results_sheet(xl)
    if sheet is None:
        return [], [f"No 'Results ALL' sheet in {xl.sheet_names}"]
    dbg.append(f"Using sheet '{sheet}'")
    raw = xl.parse(sheet, header=1)  # header on row index 1

    def col(df, idx):
        return df.iloc[:, idx] if idx < df.shape[1] else pd.Series([np.nan] * len(df))

    n = len(raw)
    for i in range(n):
        r = raw.iloc[i]
        result = str(r.iloc[15]).strip() if raw.shape[1] > 15 else ""
        if not result or result in ("nan", "None", ""):
            continue
        hotel = fix_enc(r.iloc[3]).strip() if raw.shape[1] > 3 else ""
        if not hotel or hotel.lower() in ("nan", "none", ""):
            continue
        giata = str(r.iloc[4]).strip() if raw.shape[1] > 4 else ""
        try:
            giata = str(int(float(giata)))
        except Exception:
            pass
        board = str(r.iloc[10]).strip() if raw.shape[1] > 10 else ""
        check_in = parse_date(r.iloc[5]) if raw.shape[1] > 5 else None
        nights = int(clean_num(r.iloc[7]) or 7) if raw.shape[1] > 7 else 7
        d2_price = clean_num(r.iloc[11])
        position = clean_num(r.iloc[12])
        lead_comp = str(r.iloc[13]).strip() if raw.shape[1] > 13 else ""
        comp_price = clean_num(r.iloc[14])
        diff_pct = clean_num(r.iloc[16])
        d2_live = clean_num(r.iloc[17])
        cur_m = to_pct(r.iloc[18])
        aft_m = to_pct(r.iloc[19])
        supplier = str(r.iloc[21]).strip() if raw.shape[1] > 21 else ""

        # Normalise result label
        rl = result.lower()
        if "no d2" in rl:
            result = "No D2 Result"
        elif "no comp" in rl:
            result = "Win - No Comp"
        elif "win" in rl:
            result = "Win"
        elif "lose" in rl or "loss" in rl:
            result = "Lose"
        else:
            continue

        if diff_pct is not None and not pd.isna(diff_pct) and abs(diff_pct) <= 5:
            diff_pct = diff_pct * 100  # decimal -> percentage

        cm = cur_m if not pd.isna(cur_m) else 0.0
        am = aft_m if not pd.isna(am := aft_m) else 0.0
        headroom = round(am - cm, 2) if (am and cm) else 0.0
        dep_month = check_in.strftime("%Y-%m") if check_in else ""
        dw = dep_window(check_in, file_dt)

        rows.append({
            "file_name": fname, "platform": platform, "file_date": str(file_dt),
            "destination": dest_name, "giata": giata, "hotel_name": hotel, "board": board,
            "check_in": str(check_in) if check_in else "", "dep_month": dep_month,
            "dep_window": dw, "nights": nights, "supplier": supplier,
            "position": float(position) if not pd.isna(position) else 0.0,
            "lead_competitor": lead_comp if lead_comp not in ("nan", "None", "") else "",
            "d2_price": float(d2_price) if not pd.isna(d2_price) else 0.0,
            "comp_price": float(comp_price) if not pd.isna(comp_price) else 0.0,
            "d2_live": float(d2_live) if not pd.isna(d2_live) else 0.0,
            "result": result,
            "diff_pct": float(diff_pct) if not pd.isna(diff_pct) else 0.0,
            "current_margin": cm, "margin_after": am, "margin_headroom": headroom,
            "margin_flag": margin_flag(cm),
            "uploaded_at": datetime.now().isoformat(),
        })
    dbg.append(f"Parsed {len(rows)} comparison rows for {dest_name}")
    return rows, dbg

# ══════════════════════════════════════════════════════════════════════════════
# CLICKS PARSER  (Clicks & Costs Master)
# ══════════════════════════════════════════════════════════════════════════════
def parse_clicks_file(uploaded_file, fname):
    rows, dbg = [], []
    try:
        xl = pd.ExcelFile(uploaded_file, engine="pyxlsb")
    except Exception:
        try:
            xl = pd.ExcelFile(uploaded_file, engine="openpyxl")
        except Exception as e:
            return [], [f"ERROR opening clicks file: {e}"]
    summary_sheets = [s for s in xl.sheet_names if s.strip().lower().endswith("summary")
                      and "ireland" not in s.lower()]
    if not summary_sheets:
        summary_sheets = [s for s in xl.sheet_names if "summary" in s.lower()]
    dbg.append(f"Summary sheets: {summary_sheets}")
    for s in summary_sheets:
        platform = s.replace("Summary", "").strip() or "Unknown"
        try:
            d = xl.parse(s, header=0)
        except Exception as e:
            dbg.append(f"skip {s}: {e}")
            continue
        cols = {str(c).strip().lower(): c for c in d.columns}

        def g(name):
            return cols.get(name.lower())

        name_c = g("PropertyName")
        dest_c = g("Hermes Destination") or g("Country")
        clk_c = g("Clicks")
        if name_c is None or clk_c is None:
            continue
        for _, r in d.iterrows():
            hotel = fix_enc(r.get(name_c, "")).strip()
            if not hotel or hotel.lower() in ("nan", "none", ""):
                continue
            destination = fix_enc(r.get(dest_c, "")).strip() if dest_c else ""
            # Hermes Destination like "Dubai - United Arab Emirates" -> take leading token
            destination = destination.split(" - ")[0].strip() if " - " in destination else destination
            clicks = clean_num(r.get(clk_c, 0))
            costs = clean_num(r.get(g("Costs"), 0))
            cpc = clean_num(r.get(g("Avg CPC"), 0))
            bookings = clean_num(r.get(g("Total Bookings"), 0))
            revenue = clean_num(r.get(g("Revenue"), 0))
            profit = clean_num(r.get(g("Total Profit") or g("Profit"), 0))
            spp = clean_num(r.get(g("Spend-Profit%"), 0))
            rows.append({
                "file_name": fname, "platform": platform, "hotel_name": hotel,
                "destination": destination,
                "clicks": float(clicks) if not pd.isna(clicks) else 0.0,
                "costs": float(costs) if not pd.isna(costs) else 0.0,
                "cpc": float(cpc) if not pd.isna(cpc) else 0.0,
                "bookings": float(bookings) if not pd.isna(bookings) else 0.0,
                "revenue": float(revenue) if not pd.isna(revenue) else 0.0,
                "profit": float(profit) if not pd.isna(profit) else 0.0,
                "spend_profit_pct": float(spp) if not pd.isna(spp) else 0.0,
                "uploaded_at": datetime.now().isoformat(),
            })
    dbg.append(f"Parsed {len(rows)} clicks rows")
    return rows, dbg

# ══════════════════════════════════════════════════════════════════════════════
# BOOKINGS PARSER  (transactional booking export with Giata id)
# ══════════════════════════════════════════════════════════════════════════════
def parse_bookings_file(uploaded_file, fname):
    """Read a transactional booking export and aggregate per hotel (giata)."""
    dbg = []
    try:
        if fname.lower().endswith(".csv"):
            raw = pd.read_csv(uploaded_file)
        else:
            xl = pd.ExcelFile(uploaded_file, engine="openpyxl")
            # prefer a sheet that actually holds the booking columns
            sheet = xl.sheet_names[0]
            for s in xl.sheet_names:
                head = xl.parse(s, nrows=1)
                cl = [str(c).strip().lower() for c in head.columns]
                if "giata" in cl and ("revenue" in cl or "hotels" in cl):
                    sheet = s
                    break
            raw = xl.parse(sheet)
    except Exception as e:
        return [], [], [f"ERROR opening bookings file: {e}"]

    cols = {str(c).strip().lower(): c for c in raw.columns}

    def g(*names):
        for n in names:
            if n.lower() in cols:
                return cols[n.lower()]
        return None

    giata_c = g("Giata", "giata_id", "giata id")
    hotel_c = g("Hotels", "Hotel", "Hotelname", "hotel_name")
    dest_c = g("Destination", "Dest")
    rev_c = g("Revenue", "TotalSell", "Total Sell")
    profit_c = g("Front", "TotalProfit", "Profit")
    mpct_c = g("Front.1", "Front Margin", "Margin")
    dep_c = g("Departure Date", "Departure", "Checkin", "Check In")
    booked_c = g("Booked Date", "Booked", "Booking Date", "Created")

    if giata_c is None:
        return [], [], [f"No 'Giata' column found. Columns: {list(raw.columns)[:12]}"]

    raw = raw.copy()
    raw["_giata"] = raw[giata_c].apply(
        lambda x: str(int(float(x))) if pd.notna(x) and str(x).strip() not in ("", "nan") else "")
    raw = raw[raw["_giata"] != ""]
    raw["_hotel"] = raw[hotel_c].apply(fix_enc).str.strip() if hotel_c else ""
    raw["_dest"] = raw[dest_c].apply(fix_enc).str.strip() if dest_c else ""
    raw["_rev"] = pd.to_numeric(raw[rev_c], errors="coerce") if rev_c else np.nan
    raw["_profit"] = pd.to_numeric(raw[profit_c], errors="coerce") if profit_c else np.nan
    if mpct_c:
        mp = pd.to_numeric(raw[mpct_c], errors="coerce")
        raw["_mpct"] = mp.apply(lambda v: v * 100 if pd.notna(v) and abs(v) <= 1.5 else v)
    elif rev_c and profit_c:
        raw["_mpct"] = (raw["_profit"] / raw["_rev"].replace(0, np.nan)) * 100
    else:
        raw["_mpct"] = np.nan
    if dep_c:
        raw["_dep_month"] = pd.to_datetime(raw[dep_c], errors="coerce").dt.strftime("%Y-%m").fillna("")
    else:
        raw["_dep_month"] = ""
    if booked_c:
        raw["_booked_month"] = pd.to_datetime(raw[booked_c], errors="coerce").dt.strftime("%Y-%m").fillna("")
    else:
        raw["_booked_month"] = ""

    rows = []
    for giata, grp in raw.groupby("_giata"):
        hotel = grp["_hotel"].mode().iloc[0] if hotel_c and not grp["_hotel"].mode().empty else ""
        dest = grp["_dest"].mode().iloc[0] if dest_c and not grp["_dest"].mode().empty else ""
        rows.append({
            "file_name": fname, "giata": giata, "hotel_name": hotel, "destination": dest,
            "bookings": int(len(grp)),
            "revenue": float(grp["_rev"].sum(skipna=True)) if rev_c else 0.0,
            "profit": float(grp["_profit"].sum(skipna=True)) if profit_c else 0.0,
            "avg_margin": round(float(grp["_mpct"].mean(skipna=True)), 2)
                          if grp["_mpct"].notna().any() else 0.0,
            "dep_month": "", "uploaded_at": datetime.now().isoformat(),
        })

    # month-level aggregates: one row per giata × booked_month × dep_month
    month_rows = []
    ts = datetime.now().isoformat()
    for (giata, dest, bm, dm), grp in raw.groupby(["_giata", "_dest", "_booked_month", "_dep_month"]):
        month_rows.append({
            "file_name": fname, "giata": giata, "destination": dest,
            "booked_month": bm or "", "dep_month": dm or "",
            "bookings": int(len(grp)),
            "revenue": float(grp["_rev"].sum(skipna=True)) if rev_c else 0.0,
            "profit": float(grp["_profit"].sum(skipna=True)) if profit_c else 0.0,
            "avg_margin": round(float(grp["_mpct"].mean(skipna=True)), 2)
                          if grp["_mpct"].notna().any() else 0.0,
            "uploaded_at": ts,
        })

    dbg.append(f"Parsed {len(raw)} bookings → {len(rows)} hotels across "
               f"{raw['_dest'].nunique()} destinations · {len(month_rows)} month-buckets")
    return rows, month_rows, dbg

# ══════════════════════════════════════════════════════════════════════════════
# RULE-ENGINE PARSER  (markup rules → hotel-only margin levers)
# Codes per the parsing brief:
#   RuleAppliesTo: 12 HotelRoom (keep) · 15 Package · 1 Flight (drop)
#   MarkupType:    1 Manual / 3 Exclusive (consumer levers) · 0,5 system (context)
#   RuleActionType:8 MarkupNew / 7 MarkupPerPerson (markups) · others ignored
#   ActionOperator:1 percentage · 2 fixed £
# ══════════════════════════════════════════════════════════════════════════════
def norm_giata(x):
    s = str(x).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return re.sub(r"\D", "", s)

def merge_supplier_name(s):
    """Merge supplier codes to the parent brand: strip the part after ' - ' when that
    suffix is an id/code (contains a digit), e.g. 'OTS - AMXCUN75FI' → 'OTS',
    'ResortMarketing - 90009' → 'ResortMarketing'. Named sub-suppliers with no digits
    (e.g. 'Juniper - Travco') are left intact."""
    s = str(s).strip()
    if " - " in s:
        prefix, suffix = s.split(" - ", 1)
        if re.search(r"\d", suffix):
            return prefix.strip()
    return s

def parse_rules_file(uploaded_file, fname):
    dbg = []
    try:
        if fname.lower().endswith(".csv"):
            d = pd.read_csv(uploaded_file)
        else:
            d = pd.read_excel(uploaded_file, engine="openpyxl")
    except Exception as e:
        return [], [f"ERROR opening rule export: {e}"]
    if "RuleJson" not in d.columns:
        return [], [f"No 'RuleJson' column. Columns: {list(d.columns)[:10]}"]

    rows = []
    for _, r in d.iterrows():
        try:
            j = json.loads(r["RuleJson"])
        except Exception:
            continue
        props = j.get("RuleProperties", []) or []
        act = j.get("Action", {}) or {}

        def find(t):
            return [p for p in props if p.get("RulePropertyType") == t]

        gi = find("GIata"); ip = find("IsPackage")
        giatas, giata_op = [], None
        if gi:
            giata_op = gi[0].get("ConditionOperator")
            if giata_op in ("Equal", "IsOneOf"):
                giatas = [norm_giata(v) for v in str(gi[0].get("ConditionValue", "")).split(",")
                          if norm_giata(v)]
        is_pkg = ""
        if ip:
            is_pkg = "True" if str(ip[0].get("ConditionValue", "")).strip().lower() == "true" else "False"
        applies_to = j.get("RuleAppliesTo", r.get("RuleAppliesTo"))
        mt = act.get("MarkupType")
        if mt is None or (isinstance(mt, float) and pd.isna(mt)):
            mt = r.get("MarkupType")
        atype = act.get("RuleActionType", r.get("RuleActionType"))
        date_gated = any(p.get("RulePropertyType") in ("StartDate", "EndDate", "SearchedDate")
                         for p in props)
        try:
            mt_int = int(float(mt)) if mt is not None and not (isinstance(mt, float) and pd.isna(mt)) else -1
        except Exception:
            mt_int = -1
        try:
            at_int = int(float(atype)) if atype is not None else -1
        except Exception:
            at_int = -1
        try:
            ap_int = int(float(applies_to)) if applies_to is not None else -1
        except Exception:
            ap_int = -1
        rows.append({
            "file_name": fname, "rule_id": str(r.get("Id", "")),
            "enabled": 1 if int(r.get("Enabled", 0) or 0) == 1 else 0,
            "applies_to": ap_int, "action_type": at_int, "markup_type": mt_int,
            "value": float(act.get("RuleActionPropertyValue")) if act.get("RuleActionPropertyValue") is not None else np.nan,
            "op": int(act.get("RuleActionPropertyOperator")) if act.get("RuleActionPropertyOperator") is not None else -1,
            "markup_app": int(act.get("MarkupApplication")) if act.get("MarkupApplication") is not None else -1,
            "giatas": ",".join(giatas), "giata_op": giata_op or "",
            "is_package": is_pkg, "n_conditions": len(props),
            "date_gated": 1 if date_gated else 0,
            "gen_text": str(r.get("GeneratedText", ""))[:400],
            "uploaded_at": datetime.now().isoformat(),
        })
    n_scope = sum(1 for x in rows if x["enabled"] and x["applies_to"] == 12
                  and x["action_type"] in (8, 7) and x["is_package"] != "True"
                  and x["markup_type"] in (1, 3))
    dbg.append(f"Parsed {len(rows)} rules · {n_scope} enabled hotel-only consumer markup levers")
    return rows, dbg

# ══════════════════════════════════════════════════════════════════════════════
# OFFERS PARSER  (hotel offers → live exclusive/standard status per giata)
# ══════════════════════════════════════════════════════════════════════════════
def parse_offers_file(uploaded_file, fname):
    dbg = []
    try:
        if fname.lower().endswith(".csv"):
            raw = pd.read_csv(uploaded_file)
        else:
            xl = pd.ExcelFile(uploaded_file, engine="openpyxl")
            sheet = xl.sheet_names[0]
            for s in xl.sheet_names:
                head = xl.parse(s, nrows=1)
                cl = [str(c).strip().lower() for c in head.columns]
                if "giata" in cl and any("offer" in c or "exclusive" in c for c in cl):
                    sheet = s
                    break
            raw = xl.parse(sheet)
    except Exception as e:
        return [], [f"ERROR opening offers file: {e}"]

    cols = {str(c).strip().lower(): c for c in raw.columns}

    def g(*names):
        for n in names:
            if n.lower() in cols:
                return cols[n.lower()]
        return None

    giata_c = g("Giata", "giata_id", "giata id")
    if giata_c is None:
        return [], [f"No 'Giata' column found. Columns: {list(raw.columns)[:12]}"]
    hotel_c = g("Hotel Name", "Hotels", "Hotel", "Hotelname")
    dest_c = g("Hermes Destination", "Destination", "Dest")
    type_c = g("Offer Type Name", "Offer Type", "Offer")
    excl_c = g("Is Exclusive", "Exclusive")
    status_c = g("Deal Status", "Status")
    bf_c = g("Booking From", "Book From")
    bt_c = g("Booking To", "Book To")
    tf_c = g("Travel From", "Travel from")
    tt_c = g("Travel To", "Travel to")
    det_c = g("Tactical Offer Details", "Details", "Offer Details")

    def _b(x):
        return 1 if str(x).strip().lower() in ("true", "1", "yes", "y") else 0

    def _s(x):
        return "" if pd.isna(x) else str(x).strip()

    def _d(x):
        d = pd.to_datetime(x, errors="coerce")
        return "" if pd.isna(d) else d.strftime("%Y-%m-%d")

    rows = []
    ts = datetime.now().isoformat()
    for _, r in raw.iterrows():
        gi = norm_giata(r.get(giata_c, ""))
        if not gi:
            continue
        rows.append({
            "file_name": fname, "giata": gi,
            "hotel_name": fix_enc(_s(r.get(hotel_c, ""))).strip() if hotel_c else "",
            "destination": fix_enc(_s(r.get(dest_c, ""))).strip() if dest_c else "",
            "offer_type": _s(r.get(type_c, "")) if type_c else "",
            "is_exclusive": _b(r.get(excl_c, "")) if excl_c else 0,
            "deal_status": _s(r.get(status_c, "")) if status_c else "",
            "book_from": _d(r.get(bf_c, "")) if bf_c else "",
            "book_to": _d(r.get(bt_c, "")) if bt_c else "",
            "travel_from": _d(r.get(tf_c, "")) if tf_c else "",
            "travel_to": _d(r.get(tt_c, "")) if tt_c else "",
            "details": _s(r.get(det_c, ""))[:300] if det_c else "",
            "uploaded_at": ts,
        })
    dbg.append(f"Parsed {len(rows)} offers across {len(set(x['giata'] for x in rows))} hotels")
    return rows, dbg

# ══════════════════════════════════════════════════════════════════════════════
# SAVE / LOAD
# ══════════════════════════════════════════════════════════════════════════════
def save_comparison(rows):
    if not rows:
        return 0, 0
    conn = get_conn(); c = conn.cursor(); n = skipped = 0
    for r in rows:
        c.execute("""SELECT id FROM comparison WHERE file_name=? AND giata=?
                     AND check_in=? AND board=? AND lead_competitor=?""",
                  (r["file_name"], r["giata"], r["check_in"], r["board"], r["lead_competitor"]))
        if c.fetchone() is None:
            keys = ["file_name", "platform", "file_date", "destination", "giata", "hotel_name",
                    "board", "check_in", "dep_month", "dep_window", "nights", "supplier",
                    "position", "lead_competitor", "d2_price", "comp_price", "d2_live", "result",
                    "diff_pct", "current_margin", "margin_after", "margin_headroom", "margin_flag",
                    "uploaded_at"]
            c.execute(f"""INSERT INTO comparison({','.join(keys)})
                          VALUES({','.join(['?']*len(keys))})""", tuple(r[k] for k in keys))
            n += 1
        else:
            skipped += 1
    conn.commit(); conn.close()
    return n, skipped

def save_clicks(rows):
    if not rows:
        return 0
    conn = get_conn(); c = conn.cursor()
    # Replace clicks for these files (latest upload wins)
    files = set(r["file_name"] for r in rows)
    for f in files:
        c.execute("DELETE FROM clicks WHERE file_name=?", (f,))
    keys = ["file_name", "platform", "hotel_name", "destination", "clicks", "costs", "cpc",
            "bookings", "revenue", "profit", "spend_profit_pct", "uploaded_at"]
    for r in rows:
        c.execute(f"""INSERT INTO clicks({','.join(keys)})
                      VALUES({','.join(['?']*len(keys))})""", tuple(r[k] for k in keys))
    conn.commit(); conn.close()
    return len(rows)

def save_bookings(rows):
    if not rows:
        return 0
    conn = get_conn(); c = conn.cursor()
    files = set(r["file_name"] for r in rows)
    for f in files:
        c.execute("DELETE FROM bookings WHERE file_name=?", (f,))
    keys = ["file_name", "giata", "hotel_name", "destination", "bookings", "revenue",
            "profit", "avg_margin", "dep_month", "uploaded_at"]
    for r in rows:
        c.execute(f"""INSERT INTO bookings({','.join(keys)})
                      VALUES({','.join(['?']*len(keys))})""", tuple(r[k] for k in keys))
    conn.commit(); conn.close()
    return len(rows)

def save_booking_months(rows):
    if not rows:
        return 0
    conn = get_conn(); c = conn.cursor()
    files = set(r["file_name"] for r in rows)
    for f in files:
        c.execute("DELETE FROM booking_months WHERE file_name=?", (f,))
    keys = ["file_name", "giata", "destination", "booked_month", "dep_month",
            "bookings", "revenue", "profit", "avg_margin", "uploaded_at"]
    for r in rows:
        c.execute(f"""INSERT INTO booking_months({','.join(keys)})
                      VALUES({','.join(['?']*len(keys))})""", tuple(r[k] for k in keys))
    conn.commit(); conn.close()
    return len(rows)

def save_rules(rows):
    if not rows:
        return 0
    conn = get_conn(); c = conn.cursor()
    files = set(r["file_name"] for r in rows)
    for f in files:
        c.execute("DELETE FROM rules WHERE file_name=?", (f,))
    keys = ["file_name", "rule_id", "enabled", "applies_to", "action_type", "markup_type",
            "value", "op", "markup_app", "giatas", "giata_op", "is_package",
            "n_conditions", "date_gated", "gen_text", "uploaded_at"]
    for r in rows:
        c.execute(f"""INSERT INTO rules({','.join(keys)})
                      VALUES({','.join(['?']*len(keys))})""", tuple(r[k] for k in keys))
    conn.commit(); conn.close()
    return len(rows)

def load_rules():
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM rules", conn); conn.close()
    if df.empty:
        return df
    for col in ["enabled", "applies_to", "action_type", "markup_type", "op",
                "markup_app", "n_conditions", "date_gated"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(-1).astype(int)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["giatas"] = df["giatas"].fillna("").astype(str)
    return df

def load_comparison(dest=None, platform=None, window=None, files=None):
    conn = get_conn()
    q = "SELECT * FROM comparison WHERE 1=1"; p = []
    if dest:     q += f" AND destination IN ({','.join(['?']*len(dest))})"; p += dest
    if platform: q += f" AND platform IN ({','.join(['?']*len(platform))})"; p += platform
    if window:   q += f" AND dep_window IN ({','.join(['?']*len(window))})"; p += window
    if files:    q += f" AND file_name IN ({','.join(['?']*len(files))})"; p += files
    df = pd.read_sql_query(q, conn, params=p); conn.close()
    if df.empty:
        return df
    for col in ["d2_price", "comp_price", "d2_live", "diff_pct", "current_margin",
                "margin_after", "margin_headroom", "position"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["check_in_dt"] = pd.to_datetime(df["check_in"].astype(str), errors="coerce")
    return df

def load_clicks():
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM clicks", conn); conn.close()
    if df.empty:
        return df
    for col in ["clicks", "costs", "cpc", "bookings", "revenue", "profit", "spend_profit_pct"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df

def load_bookings():
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM bookings", conn); conn.close()
    if df.empty:
        return df
    for col in ["bookings", "revenue", "profit", "avg_margin"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["giata"] = df["giata"].astype(str)
    return df

def load_booking_months():
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM booking_months", conn); conn.close()
    if df.empty:
        return df
    for col in ["bookings", "revenue", "profit", "avg_margin"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["giata"] = df["giata"].astype(str)
    df["booked_month"] = df["booked_month"].fillna("").astype(str)
    df["dep_month"] = df["dep_month"].fillna("").astype(str)
    return df

def bookings_by_month(tx, giatas, dim):
    """Filter the booking time-series to a set of giatas and group by `dim`
    ('dep_month' or 'booked_month') → Month, Bookings, Revenue."""
    empty = pd.DataFrame(columns=["Month", "Bookings", "Revenue"])
    if tx is None or tx.empty or dim not in ("dep_month", "booked_month"):
        return empty
    t = tx[tx["giata"].astype(str).isin(set(str(x) for x in giatas))].copy()
    t = t[t[dim] != ""]
    if t.empty:
        return empty
    out = t.groupby(dim, as_index=False).agg(Bookings=("bookings", "sum"),
                                             Revenue=("revenue", "sum"))
    out.columns = ["Month", "Bookings", "Revenue"]
    return out.sort_values("Month")

def save_offers(rows):
    if not rows:
        return 0
    conn = get_conn(); c = conn.cursor()
    files = set(r["file_name"] for r in rows)
    for f in files:
        c.execute("DELETE FROM offers WHERE file_name=?", (f,))
    keys = ["file_name", "giata", "hotel_name", "destination", "offer_type", "is_exclusive",
            "deal_status", "book_from", "book_to", "travel_from", "travel_to", "details",
            "uploaded_at"]
    for r in rows:
        c.execute(f"""INSERT INTO offers({','.join(keys)})
                      VALUES({','.join(['?']*len(keys))})""", tuple(r[k] for k in keys))
    conn.commit(); conn.close()
    return len(rows)

def load_offers():
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM offers", conn); conn.close()
    if df.empty:
        return df
    df["giata"] = df["giata"].astype(str)
    df["is_exclusive"] = pd.to_numeric(df["is_exclusive"], errors="coerce").fillna(0).astype(int)
    for c in ["book_from", "book_to", "travel_from", "travel_to", "deal_status"]:
        df[c] = df[c].fillna("").astype(str)
    return df

def save_completions(rows):
    """Upsert completion rows keyed on (giata, data_date) — updates in place, so two
    people saving the same hotel/date don't duplicate; a new data_date adds history."""
    if not rows:
        return 0
    conn = get_conn(); c = conn.cursor()
    for r in rows:
        c.execute("""INSERT INTO completions
            (giata, hotel_name, destination, data_date, done, suggested_action, note, initials, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(giata, data_date) DO UPDATE SET
                done=excluded.done, suggested_action=excluded.suggested_action,
                note=excluded.note, initials=excluded.initials,
                updated_at=excluded.updated_at, hotel_name=excluded.hotel_name,
                destination=excluded.destination""",
            (str(r["giata"]), r.get("hotel_name", ""), r.get("destination", ""),
             str(r["data_date"]), int(r.get("done", 0)), r.get("suggested_action", ""),
             r.get("note", ""), r.get("initials", ""), datetime.now().isoformat(timespec="seconds")))
    conn.commit(); conn.close()
    return len(rows)

def load_completions():
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM completions", conn); conn.close()
    if df.empty:
        return df
    df["giata"] = df["giata"].astype(str)
    df["done"] = pd.to_numeric(df["done"], errors="coerce").fillna(0).astype(int)
    return df

def latest_done_by_giata(comp_log=None):
    """giata → most recent data_date that hotel was marked done (for the ✅ Done column)."""
    df = comp_log if comp_log is not None else load_completions()
    if df is None or df.empty:
        return {}
    d = df[df["done"] == 1]
    if d.empty:
        return {}
    return d.groupby("giata")["data_date"].max().astype(str).to_dict()

def clear_completions():
    conn = get_conn(); conn.execute("DELETE FROM completions"); conn.commit(); conn.close()

def offer_status_by_giata(offers_df, today=None):
    """Per-giata live-offer status: '🌟 Exclusive', 'Offer' (standard) or '' (none).
    Live = Deal Status Active/blank, today within booking window, travel still ahead."""
    if offers_df is None or offers_df.empty:
        return {}
    today = pd.Timestamp(today or pd.Timestamp.today().normalize())

    def _dt(s):
        d = pd.to_datetime(s, errors="coerce")
        return d

    status = {}
    for gi, grp in offers_df.groupby("giata"):
        has_excl = has_std = False
        for _, r in grp.iterrows():
            ds = str(r.get("deal_status", "")).strip().lower()
            if ds == "nan":
                ds = ""
            if ds not in ("active", ""):
                continue
            bf, bt, tt = _dt(r.get("book_from")), _dt(r.get("book_to")), _dt(r.get("travel_to"))
            if pd.notna(bf) and bf > today:
                continue
            if pd.notna(bt) and bt < today:
                continue
            if pd.notna(tt) and tt < today:
                continue
            if int(r.get("is_exclusive", 0)) == 1:
                has_excl = True
            else:
                has_std = True
        if has_excl:
            status[str(gi)] = "🌟 Exclusive"
        elif has_std:
            status[str(gi)] = "Offer"
    return status

def get_distinct(col):
    conn = get_conn()
    try:
        rows = conn.execute(f"SELECT DISTINCT {col} FROM comparison ORDER BY {col}").fetchall()
    except Exception:
        rows = []
    conn.close()
    return [r[0] for r in rows if r[0]]

def get_file_list():
    conn = get_conn()
    rows = conn.execute("""SELECT file_name, platform, file_date, destination, COUNT(*)
        FROM comparison GROUP BY file_name ORDER BY file_date DESC""").fetchall()
    conn.close()
    return rows

# ══════════════════════════════════════════════════════════════════════════════
# JOIN CLICKS -> COMPARISON (fuzzy on hotel name within destination)
# ══════════════════════════════════════════════════════════════════════════════
def match_clicks(comp_df, clicks_df):
    """Attach per-hotel clicks/cost/bookings to each comparison row."""
    if comp_df.empty:
        return comp_df
    out = comp_df.copy()
    for c in ["clicks", "costs", "cpc", "ms_bookings", "ms_revenue", "ms_profit",
              "spend_profit_pct"]:
        out[c] = 0.0
    out["click_match"] = ""
    if clicks_df.empty:
        return out

    # Prefer clicks from the same platform as the comparison data (e.g. Trivago→Trivago).
    cl = clicks_df.copy()
    comp_platforms = set(str(p).lower() for p in out["platform"].unique())
    same = cl[cl["platform"].astype(str).str.lower().isin(comp_platforms)]
    if not same.empty:
        cl = same
    cl["destination"] = cl["destination"].astype(str)
    by_dest = {}
    for d, grp in cl.groupby("destination"):
        by_dest[d.lower()] = grp.reset_index(drop=True)
    all_clicks = cl.reset_index(drop=True)

    cache = {}
    def find(dest, hotel):
        key = (dest.lower(), hotel.lower())
        if key in cache:
            return cache[key]
        # candidate pool: same destination token first, else all
        pool = None
        for dk, grp in by_dest.items():
            if dest.lower() in dk or dk in dest.lower():
                pool = grp; break
        if pool is None or pool.empty:
            pool = all_clicks
        names = pool["hotel_name"].tolist()
        rec = None
        if HAS_FUZZ and names:
            m = process.extractOne(hotel, names, scorer=fuzz.token_sort_ratio)
            if m and m[1] >= FUZZ_THRESHOLD:
                rec = pool.iloc[m[2]]
        elif names:
            for j, nm in enumerate(names):
                if hotel.lower() == str(nm).lower():
                    rec = pool.iloc[j]; break
        cache[key] = rec
        return rec

    for idx, row in out.iterrows():
        rec = find(str(row["destination"]), str(row["hotel_name"]))
        if rec is not None:
            out.at[idx, "clicks"] = float(rec["clicks"])
            out.at[idx, "costs"] = float(rec["costs"])
            out.at[idx, "cpc"] = float(rec["cpc"])
            out.at[idx, "ms_bookings"] = float(rec["bookings"])
            out.at[idx, "ms_revenue"] = float(rec["revenue"])
            out.at[idx, "ms_profit"] = float(rec["profit"])
            out.at[idx, "spend_profit_pct"] = float(rec["spend_profit_pct"])
            out.at[idx, "click_match"] = str(rec["hotel_name"])
    return out

def attach_bookings(comp_df, bookings_df):
    """Attach actual booking volume / revenue / margin per hotel via exact GIATA match."""
    if comp_df.empty:
        return comp_df
    out = comp_df.copy()
    for c in ["bk_bookings", "bk_revenue", "bk_profit", "bk_margin"]:
        out[c] = 0.0
    out["bk_match"] = False
    if bookings_df is None or bookings_df.empty:
        return out
    bk = bookings_df.copy()
    bk["giata"] = bk["giata"].astype(str)
    # collapse to one record per giata (in case multiple files)
    bk = bk.groupby("giata", as_index=False).agg(
        bookings=("bookings", "sum"), revenue=("revenue", "sum"),
        profit=("profit", "sum"), avg_margin=("avg_margin", "mean"))
    lut = bk.set_index("giata").to_dict("index")
    g = out["giata"].astype(str)
    out["bk_bookings"] = g.map(lambda x: lut.get(x, {}).get("bookings", 0.0)).astype(float)
    out["bk_revenue"] = g.map(lambda x: lut.get(x, {}).get("revenue", 0.0)).astype(float)
    out["bk_profit"] = g.map(lambda x: lut.get(x, {}).get("profit", 0.0)).astype(float)
    out["bk_margin"] = g.map(lambda x: lut.get(x, {}).get("avg_margin", 0.0)).astype(float)
    out["bk_match"] = g.isin(lut.keys())
    return out

# ══════════════════════════════════════════════════════════════════════════════
# RULE LEVERS — turn a per-hotel margin Δ into the exact rule to edit / create
# ══════════════════════════════════════════════════════════════════════════════
def build_rule_index(rules_df):
    """Return (consumer_by_giata, system_by_giata) dicts of in-scope hotel-only rules."""
    if rules_df is None or rules_df.empty:
        return {}, {}
    r = rules_df.copy()
    r = r[(r["enabled"] == 1) & (r["applies_to"] == 12) &
          (r["action_type"].isin([8, 7])) & (r["is_package"] != "True")]
    consumer, system = {}, {}
    for _, row in r.iterrows():
        gl = [x for x in str(row["giatas"]).split(",") if x]
        if not gl:
            continue
        rec = row.to_dict()
        rec["_giatas"] = gl
        target = consumer if row["markup_type"] in (1, 3) else (
            system if row["markup_type"] in (0, 5) else None)
        if target is None:
            continue
        for g in gl:
            target.setdefault(g, []).append(rec)
    return consumer, system

def lever_for_hotel(consumer_by_giata, system_by_giata, giata, delta):
    """Per the brief: prefer editing a clean single-hotel consumer rule, else propose
    a new single-hotel rule. Shared (multi-hotel) and system rules are context only."""
    g = norm_giata(giata)
    cons = consumer_by_giata.get(g, [])
    system = system_by_giata.get(g, [])
    single = [r for r in cons if len(r["_giatas"]) == 1]
    shared = [r for r in cons if len(r["_giatas"]) > 1]

    if abs(delta) < 0.5:
        return {"action": "HOLD", "rule_id": "", "cur": np.nan, "new": np.nan, "unit": "",
                "text": "Hold — no change suggested", "shared": shared, "system": system}

    if single:
        # prefer Exclusive (3) over Manual (1); then fewest extra conditions
        best = sorted(single, key=lambda r: (r["markup_type"] != 3, r["n_conditions"]))[0]
        is_pct = best["op"] == 1
        cur = best["value"]
        new = round(cur + delta, 2) if (is_pct and pd.notna(cur)) else np.nan
        layer = "Exclusive" if best["markup_type"] == 3 else "Manual"
        unit = "%" if is_pct else "£"
        note = "" if is_pct else " (£ rule — adjust manually)"
        if best["date_gated"]:
            note += " ⏰date-gated"
        return {"action": "EDIT", "rule_id": str(best["rule_id"]), "cur": cur, "new": new,
                "unit": unit, "layer": layer, "text": best["gen_text"] + note,
                "shared": shared, "system": system}

    # no single-hotel consumer rule → propose a new one (stacks additively)
    proposed = (f"If (GIata Equal {g}) And (User Equal Destination2) And "
                f"(IsPackage Equal False) Then MarkupNew HotelRoom By {delta:+.1f}% Total")
    return {"action": "ADD NEW", "rule_id": "", "cur": np.nan, "new": delta, "unit": "%",
            "layer": "Manual", "text": proposed, "shared": shared, "system": system}

def attach_rule_levers(hotels_df, rules_df):
    """Enrich the hotel aggregate with the concrete rule action for each suggested Δ."""
    if hotels_df.empty:
        return hotels_df
    out = hotels_df.copy()
    for c, default in [("rule_action", "—"), ("rule_id", ""), ("rule_cur", np.nan),
                       ("rule_new", np.nan), ("rule_unit", ""), ("rule_text", ""),
                       ("rule_shared_ct", 0), ("rule_system_ct", 0)]:
        out[c] = default
    if rules_df is None or rules_df.empty:
        return out
    cons_idx, sys_idx = build_rule_index(rules_df)
    for idx, row in out.iterrows():
        lev = lever_for_hotel(cons_idx, sys_idx, row["giata"], row.get("sug_delta", 0.0))
        out.at[idx, "rule_action"] = lev["action"]
        out.at[idx, "rule_id"] = lev["rule_id"]
        out.at[idx, "rule_cur"] = lev["cur"]
        out.at[idx, "rule_new"] = lev["new"]
        out.at[idx, "rule_unit"] = lev["unit"]
        out.at[idx, "rule_text"] = lev["text"]
        out.at[idx, "rule_shared_ct"] = len(lev["shared"])
        out.at[idx, "rule_system_ct"] = len(lev["system"])
    return out

# ══════════════════════════════════════════════════════════════════════════════
# HOTEL-LEVEL AGGREGATION + DECISION MATRIX
# ══════════════════════════════════════════════════════════════════════════════
def suggest_margin(grp, base_margin):
    """
    Part 1 of the logic brief — win/loss → suggested markup change.

    Per hotel, across all its departure dates. Uses only Win/Lose rows (Win-No-Comp
    excluded), latest run only. Models price sensitivity: changing markup by Δpp shifts
    a row's gap by Δ × 100/(100+markup). A "loss" within NOISE_GAP of break-even is a
    draw (never chased), so the win test is gap > -NOISE_GAP throughout.

    Two paths:
      • win rate ≥ WR_HIGH → RAISE to capture margin: largest raise keeping projected
        win rate ≥ WR_KEEP and flipping ≤ FLIP_FRAC of solid wins to real losses.
      • otherwise → balanced: maximise expected booked margin = proj_win_rate × proj_markup,
        then prefer the smallest move (give away the least margin).
    Guardrails: never push any row below MARGIN_FLOOR or above MARGIN_CEILING; ±BAND max.
    """
    comp = grp[grp["result"].isin(["Win", "Lose"])].copy()
    m0_disp = float(base_margin) if (base_margin and not pd.isna(base_margin)) else 0.0

    if comp.empty:
        return {"current": round(m0_disp, 1), "suggested": round(m0_disp, 1), "delta": 0.0,
                "cur_wr": np.nan, "pred_wr": np.nan, "note": "No direct comp — destination-guided"}

    # latest run only, if the same departures were compared on multiple run dates
    if "file_date" in comp and comp["file_date"].nunique() > 1:
        comp = comp[comp["file_date"] == comp["file_date"].max()]

    m = pd.to_numeric(comp["current_margin"], errors="coerce")
    g = pd.to_numeric(comp["diff_pct"], errors="coerce")
    ok = m.notna() & g.notna() & (m > 0)
    m = m[ok].to_numpy(); g = g[ok].to_numpy()
    if len(m) == 0:
        return {"current": round(m0_disp, 1), "suggested": round(m0_disp, 1), "delta": 0.0,
                "cur_wr": np.nan, "pred_wr": np.nan, "note": "No direct comp — destination-guided"}

    m0 = float(np.mean(m))
    factor = 100.0 / (100.0 + np.maximum(m, 0.0))     # %price per 1pp markup, per row
    wr0 = float(np.mean(g > -NOISE_GAP) * 100.0)      # noise-tolerant win rate

    d_min = -max(0.0, float(np.min(m)) - MARGIN_FLOOR)    # don't push any row below floor
    d_max = max(0.0, MARGIN_CEILING - float(np.max(m)))   # don't push any row above ceiling
    d_min, d_max = max(d_min, -BAND), min(d_max, BAND)

    def result(delta, wr1, m1, note):
        delta = round(float(delta), 1)
        m1 = round(m0_disp + delta, 1)                 # reconcile with displayed current margin
        return {"current": round(m0_disp, 1), "suggested": m1, "delta": delta,
                "cur_wr": wr0 / 100.0, "pred_wr": float(min(max(wr1, 0.0), 100.0)) / 100.0,
                "note": note}

    if d_max - d_min < STEP:
        return result(0.0, wr0, m0, "At margin guardrail — hold")

    # ---- Path A: high win rate → raise to capture margin ----
    if wr0 >= WR_HIGH:
        solid = int(np.sum(g > NOISE_GAP))             # wins with genuine headroom
        cap = int(np.floor(FLIP_FRAC * solid))
        delta, wr1 = 0.0, wr0
        d = STEP
        while d <= d_max + 1e-9:
            new = g - d * factor
            wr1c = float(np.mean(new > -NOISE_GAP) * 100.0)
            flips = int(np.sum((g > NOISE_GAP) & (new <= -NOISE_GAP)))
            if wr1c >= WR_KEEP and flips <= cap:
                delta, wr1 = d, wr1c                   # keep the LARGEST feasible raise
            d += STEP
        note = ("High win rate — raise to capture margin" if delta >= 0.05
                else "High win rate — hold")
        return result(delta, wr1, m0 + delta, note)

    # ---- Path B: contested → balanced (expected booked margin) ----
    cands = []
    d = d_min
    while d <= d_max + 1e-9:
        m1 = m0 + d
        if m1 >= MARGIN_FLOOR:
            wr1 = float(np.mean((g - d * factor) > -NOISE_GAP) * 100.0)
            cands.append((d, wr1, m1, (wr1 / 100.0) * m1))
        d += STEP
    if not cands:
        return result(0.0, wr0, m0, "At margin guardrail — hold")
    best_obj = max(c[3] for c in cands)
    near = [c for c in cands if c[3] >= best_obj - 0.02]   # treat ~ties as equal
    pick = min(near, key=lambda c: (abs(c[0]), -c[2]))     # smallest move, then higher margin
    delta, wr1 = pick[0], pick[1]
    n_loss_flip = int(np.sum((g <= -NOISE_GAP) & ((g - delta * factor) > -NOISE_GAP)))
    if delta <= -0.05:
        note = f"Cut to flip {n_loss_flip} loss(es)" if n_loss_flip else f"Reduce {abs(delta):.1f}pp"
    elif delta >= 0.05:
        note = "Headroom to raise"
    elif wr0 < 50:
        note = "Product-led — price won't fix (see Product/Cost)"
    else:
        note = "Maintain — already optimal"
    return result(delta, wr1, m0 + delta, note)


def derive_markup_to_match(current_markup, gap):
    """markup-to-match (margin_after) fallback per the brief, when not supplied:
    current_markup − |gap| × (1 + current_markup/100)."""
    try:
        cm = float(current_markup); gp = float(gap)
    except Exception:
        return np.nan
    return cm - abs(gp) * (1 + cm / 100.0)


def aggregate_hotels(df):
    """One row per hotel × destination with price stance, margin, clicks."""
    if df.empty:
        return pd.DataFrame()
    comp = df[df["result"].isin(["Win", "Lose"])]
    g = df.groupby(["giata", "hotel_name", "destination"])
    recs = []
    for (giata, hotel, dest), grp in g:
        wins = (grp["result"] == "Win").sum()
        loses = (grp["result"] == "Lose").sum()
        nocomp = (grp["result"] == "Win - No Comp").sum()
        nores = (grp["result"] == "No D2 Result").sum()
        head2head = wins + loses
        win_rate = wins / head2head if head2head else np.nan
        h2h = grp[grp["result"].isin(["Win", "Lose"])]
        avg_diff = h2h["diff_pct"].mean() if not h2h.empty else np.nan
        cur_m = grp[grp["current_margin"] > 0]["current_margin"].mean()
        aft_m = grp[grp["margin_after"] > 0]["margin_after"].mean()
        cur_m = cur_m if not pd.isna(cur_m) else 0.0
        aft_m = aft_m if not pd.isna(aft_m) else 0.0
        clicks = grp["clicks"].max() if "clicks" in grp else 0.0
        costs = grp["costs"].max() if "costs" in grp else 0.0
        cpc = grp["cpc"].max() if "cpc" in grp else 0.0
        ms_book = grp["ms_bookings"].max() if "ms_bookings" in grp else 0.0
        ms_profit = grp["ms_profit"].max() if "ms_profit" in grp else 0.0
        bk_book = grp["bk_bookings"].max() if "bk_bookings" in grp else 0.0
        bk_rev = grp["bk_revenue"].max() if "bk_revenue" in grp else 0.0
        bk_profit = grp["bk_profit"].max() if "bk_profit" in grp else 0.0
        bk_margin = grp[grp["bk_margin"] > 0]["bk_margin"].max() if "bk_margin" in grp else 0.0
        bk_margin = bk_margin if not pd.isna(bk_margin) else 0.0
        # price stance
        if head2head == 0:
            stance = "No Comparison"
        elif win_rate >= 0.5 and (pd.isna(avg_diff) or avg_diff >= -HOLD_BAND):
            stance = "Winning"
        elif win_rate < 0.5 or (not pd.isna(avg_diff) and avg_diff < -HOLD_BAND):
            stance = "Losing"
        else:
            stance = "Level"
        sug = suggest_margin(grp, cur_m)

        # Part 2 — product-gap stats (losses unreachable on price)
        loss_rows = grp[grp["result"] == "Lose"].copy()
        n_loss = len(loss_rows)
        mtm_vals, below = [], 0
        avg_loss_gap = np.nan
        if n_loss:
            avg_loss_gap = pd.to_numeric(loss_rows["diff_pct"], errors="coerce").mean()
            for _, lr in loss_rows.iterrows():
                mtm = pd.to_numeric(lr.get("margin_after"), errors="coerce")
                if pd.isna(mtm) or mtm == 0:
                    mtm = derive_markup_to_match(lr.get("current_margin"), lr.get("diff_pct"))
                if not pd.isna(mtm):
                    mtm_vals.append(float(mtm))
                    if 0 < mtm < MARGIN_FLOOR:    # >0 guard: blank/zero = unknown, not below-floor
                        below += 1
        share_below = (below / n_loss) if n_loss else 0.0
        avg_mtm = float(np.mean(mtm_vals)) if mtm_vals else np.nan
        product_gap = bool(share_below >= PRODUCT_GAP_SHARE and below >= PRODUCT_GAP_MIN)

        recs.append({
            "giata": giata, "hotel_name": hotel, "destination": dest,
            "searches": len(grp), "wins": wins, "loses": loses,
            "nocomp": nocomp, "nores": nores,
            "win_rate": win_rate, "avg_diff_pct": avg_diff,
            "current_margin": round(cur_m, 2), "margin_after": round(aft_m, 2),
            "margin_headroom": round(aft_m - cur_m, 2) if (aft_m and cur_m) else 0.0,
            "clicks": clicks, "costs": costs, "cpc": cpc,
            "ms_bookings": ms_book, "ms_profit": ms_profit,
            "bk_bookings": bk_book, "bk_revenue": bk_rev, "bk_profit": bk_profit,
            "bk_margin": round(bk_margin, 1),
            "price_stance": stance,
            "sug_margin": sug["suggested"], "sug_delta": sug["delta"],
            "sug_cur_wr": sug["cur_wr"], "sug_pred_wr": sug["pred_wr"], "sug_note": sug["note"],
            "n_below_floor": below, "pct_below_floor": round(share_below * 100, 0),
            "avg_loss_gap": avg_loss_gap, "avg_mtm": avg_mtm, "product_gap": product_gap,
        })
    out = pd.DataFrame(recs)
    if out.empty:
        return out
    # click level relative to destination
    out["click_level"] = "mid"
    for dest, grp in out.groupby("destination"):
        present = grp[grp["clicks"] > 0]["clicks"]
        if len(present) >= 4:
            lo, hi = present.quantile(0.33), present.quantile(0.66)
        else:
            lo, hi = 5, 30
        for idx in grp.index:
            cv = out.at[idx, "clicks"]
            if cv <= max(lo, 3):
                out.at[idx, "click_level"] = "low"
            elif cv >= hi:
                out.at[idx, "click_level"] = "high"
            else:
                out.at[idx, "click_level"] = "mid"
    out["quadrant"], out["decision"] = zip(*out.apply(classify_quadrant, axis=1))
    # Demand score for prioritisation. Use actual bookings if present, else metasearch.
    def _norm(s):
        s = s.astype(float)
        mx = s.max()
        return s / mx if mx and mx > 0 else s * 0.0
    book_col = "bk_bookings" if ("bk_bookings" in out and out["bk_bookings"].sum() > 0) else "ms_bookings"
    out["demand_score"] = (0.6 * _norm(out[book_col]) +
                           0.4 * _norm(out["clicks"])).round(3)
    return out

def can_reduce(row):
    """True if we can cut price toward competitive and still hold >= floor."""
    ma = row["margin_after"]
    cm = row["current_margin"]
    # margin_after for a losing hotel is the margin if we drop to competitive price
    ref = ma if ma > 0 else cm
    return ref >= MARGIN_FLOOR

def classify_quadrant(row):
    stance = row["price_stance"]
    clk = row["click_level"]
    cm = row["current_margin"]
    headroom = row["margin_headroom"]
    high_clicks = clk == "high"
    low_clicks = clk == "low"

    # Part 2 — a flagged product gap is unreachable on price, regardless of clicks
    if row.get("product_gap", False):
        return ("Q4 · Product/Cost Fix",
                "🚫 Product gap — most losses unreachable on price (below floor) → cost/contracting")

    if stance == "Winning":
        if low_clicks:
            return ("Q1 · Visibility Gap",
                    "In on price & winning but NOT getting clicks → fix visibility / content / position (not price)")
        if high_clicks and (headroom > 1 or cm < MARGIN_CEILING):
            return ("Q2 · Raise Margin",
                    f"Winning + high clicks + headroom → RAISE price/margin (room to +{max(headroom,0):.1f}pp)")
        if high_clicks:
            return ("Q2 · Hold (capped)",
                    "Winning + high clicks but at/near ceiling → MAINTAIN, monitor")
        return ("Maintain", "Winning with mid clicks → hold position, watch competitors")

    if stance == "Losing":
        reducible = can_reduce(row)
        if low_clicks and reducible:
            return ("Q3 · Reduce Price",
                    "Losing + low clicks + margin room → REDUCE price to get competitive (low risk)")
        if low_clicks and not reducible:
            return ("Q4 · Product/Cost Fix",
                    "Losing + low clicks + at margin floor → can't cut → renegotiate cost / contracting")
        if high_clicks and reducible:
            return ("Reduce to Convert",
                    "Losing but still getting clicks + margin room → REDUCE to convert demand")
        if high_clicks and not reducible:
            return ("Cost Fix (high demand)",
                    "Losing + high clicks + no margin room → high demand, fix cost base to compete")
        return ("Review", "Losing, mid clicks → selective price review")

    if stance == "No Comparison":
        return ("No Comp", "No competitor on Trivago → destination-guided pricing; protect margin")
    return ("Review", "Level on price → optimise margin / monitor")

QUADRANT_ORDER = ["Q2 · Raise Margin", "Q3 · Reduce Price", "Q1 · Visibility Gap",
                  "Q4 · Product/Cost Fix", "Reduce to Convert", "Cost Fix (high demand)",
                  "Q2 · Hold (capped)", "Maintain", "No Comp", "Review"]

# ══════════════════════════════════════════════════════════════════════════════
# FILTER BAR
# ══════════════════════════════════════════════════════════════════════════════
def tab_filters(df, key, show_hotel=False):
    all_dest = sorted(df["destination"].unique().tolist())
    all_win = ["0-60", "61-120", "121-240", "241+"]
    if show_hotel:
        c1, c2, c3, c4 = st.columns([2, 2, 3, 2])
    else:
        c1, c2 = st.columns([2, 3])
    sel_dest = c1.multiselect("Destination", all_dest, default=all_dest, key=f"{key}_d")
    win_col = c2 if not show_hotel else c2
    sel_win = win_col.multiselect("Dep Window", all_win, default=all_win, key=f"{key}_w")
    f = df.copy()
    if sel_dest:
        f = f[f["destination"].isin(sel_dest)]
    if sel_win:
        f = f[f["dep_window"].isin(sel_win)]
    if show_hotel:
        boards = ["All"] + sorted(f["board"].dropna().unique().tolist())
        sel_b = c3.selectbox("Board", boards, key=f"{key}_b")
        results = ["All", "Win", "Lose", "Win - No Comp", "No D2 Result"]
        sel_r = c4.selectbox("Result", results, key=f"{key}_r")
        if sel_b != "All":
            f = f[f["board"] == sel_b]
        if sel_r != "All":
            f = f[f["result"] == sel_r]
    return f

# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## 🏨 D2 Trivago Intelligence")
    st.markdown("---")
    st.markdown("### 📂 Trivago Comparison Files")
    up_comp = st.file_uploader("Trivago_<DEST>_<DD_MM_YY>.xlsm (Results ALL)",
                               type=["xlsm", "xlsx"], accept_multiple_files=True, key="cu")
    if up_comp:
        for uf in up_comp:
            with st.spinner(f"Parsing {uf.name}…"):
                rws, dbg = parse_comparison_file(uf, uf.name)
            if not rws:
                st.error(f"❌ {uf.name}: 0 rows — {dbg[-1] if dbg else ''}")
                continue
            n, sk = save_comparison(rws)
            st.success(f"✅ {uf.name}: {n} added, {sk} skipped")

    st.markdown("### 🖱 Clicks & Costs Master")
    up_clk = st.file_uploader("Clicks_and_Costs_Master…xlsb / xlsx",
                              type=["xlsb", "xlsx", "xlsm"], key="ku")
    if up_clk:
        with st.spinner(f"Parsing {up_clk.name}…"):
            crows, cdbg = parse_clicks_file(up_clk, up_clk.name)
        if crows:
            saved = save_clicks(crows)
            plats = sorted(set(r["platform"] for r in crows))
            st.success(f"✅ {saved} hotel-click rows ({', '.join(plats)})")
        else:
            st.error(f"❌ No clicks parsed — {cdbg[-1] if cdbg else ''}")

    st.markdown("### 📊 Hotel Booking Data")
    bfile = st.file_uploader("Booking export (xlsx/csv) with a Giata column",
                             type=["csv", "xlsx", "xlsm"], key="bu")
    if bfile:
        with st.spinner(f"Parsing {bfile.name}…"):
            brows, bmonths, bdbg = parse_bookings_file(bfile, bfile.name)
        if brows:
            saved = save_bookings(brows)
            save_booking_months(bmonths)
            total_bk = sum(int(r["bookings"]) for r in brows)
            st.success(f"✅ {total_bk:,} bookings → {saved} hotels (matched by Giata)")
        else:
            st.error(f"❌ No bookings parsed — {bdbg[-1] if bdbg else ''}")

    st.markdown("### 🛠 Markup Rule Engine")
    rfile = st.file_uploader("Rule export (xlsx/csv) with RuleJson",
                             type=["csv", "xlsx", "xlsm"], key="ru")
    if rfile:
        with st.spinner(f"Parsing {rfile.name}…"):
            rrows, rdbg = parse_rules_file(rfile, rfile.name)
        if rrows:
            saved = save_rules(rrows)
            st.success(f"✅ {rdbg[-1]}")
        else:
            st.error(f"❌ No rules parsed — {rdbg[-1] if rdbg else ''}")

    st.markdown("### 🌟 Hotel Offers")
    ofile = st.file_uploader("Offers export (xlsx/csv) with Giata + Offer columns",
                             type=["csv", "xlsx", "xlsm"], key="ofu")
    if ofile:
        with st.spinner(f"Parsing {ofile.name}…"):
            orows, odbg = parse_offers_file(ofile, ofile.name)
        if orows:
            save_offers(orows)
            st.success(f"✅ {odbg[-1]}")
        else:
            st.error(f"❌ No offers parsed — {odbg[-1] if odbg else ''}")

    st.markdown("---")
    st.markdown("### 🔽 Global Filters")
    all_d = get_distinct("destination")
    all_p = get_distinct("platform")
    all_f = [r[0] for r in get_file_list()]
    sel_d = st.multiselect("Destination", all_d, default=all_d)
    sel_p = st.multiselect("Platform", all_p, default=all_p)
    sel_w = st.multiselect("Dep Window", ["0-60", "61-120", "121-240", "241+"],
                           default=["0-60", "61-120", "121-240", "241+"])
    sel_f = st.multiselect("Files", all_f, default=all_f)
    st.markdown("---")
    if st.button("🗑 Clear ALL data"):
        conn = get_conn()
        conn.execute("DELETE FROM comparison"); conn.execute("DELETE FROM clicks")
        conn.execute("DELETE FROM bookings"); conn.execute("DELETE FROM rules")
        conn.execute("DELETE FROM booking_months"); conn.execute("DELETE FROM offers")
        conn.commit(); conn.close()
        st.success("Cleared"); st.rerun()
    st.markdown("**🔍 DB Status**")
    try:
        conn = get_conn()
        rc = conn.execute("SELECT COUNT(*) FROM comparison").fetchone()[0]
        kc = conn.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
        bc = conn.execute("SELECT COALESCE(SUM(bookings),0) FROM bookings").fetchone()[0]
        bh = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        ru = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        of = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        conn.close()
        st.write(f"Comparison rows: {rc}")
        st.write(f"Clicks rows: {kc}")
        st.write(f"Bookings: {int(bc):,} ({bh} hotels)")
        st.write(f"Markup rules: {ru}")
        st.write(f"Offers: {of}")
    except Exception as e:
        st.write(f"DB error: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# LOAD + ENRICH
# ══════════════════════════════════════════════════════════════════════════════
df = load_comparison(dest=sel_d or None, platform=sel_p or None,
                     window=sel_w or None, files=sel_f or None)
clicks_df = load_clicks()
bookings_df = load_bookings()
rules_df = load_rules()
bmonths_df = load_booking_months()
offers_df = load_offers()
offer_status = offer_status_by_giata(offers_df)
comp_log = load_completions()
done_map = latest_done_by_giata(comp_log)

def with_offer(show_df):
    """Insert an 'Offer' column (🌟 Exclusive / Offer / blank) after 'Dest', keyed on Giata."""
    if not offer_status or "Giata" not in show_df.columns or "Offer" in show_df.columns:
        return show_df
    vals = show_df["Giata"].astype(str).map(offer_status).fillna("")
    if not vals.str.len().gt(0).any():
        return show_df
    pos = (list(show_df.columns).index("Dest") + 1) if "Dest" in show_df.columns else 1
    show_df.insert(pos, "Offer", vals)
    return show_df

def with_done(show_df):
    """Append a '✅ Done' column showing the latest pricing-data date a hotel was actioned."""
    if not done_map or "Giata" not in show_df.columns or "✅ Done" in show_df.columns:
        return show_df
    vals = show_df["Giata"].astype(str).map(done_map).fillna("")
    if not vals.str.len().gt(0).any():
        return show_df
    show_df["✅ Done"] = vals
    return show_df

def annotate(show_df):
    """Apply both the Offer and ✅ Done annotation columns."""
    return with_done(with_offer(show_df))

def progress_editor(scope, key_prefix, label="✅ Log progress for these hotels (tick, add initials & note, save)"):
    """Inline completion editor reused on every hotel-table tab, so progress can be logged
    without switching to the Progress tab. `scope` needs columns giata, hotel_name,
    destination, suggested. Writes to the same shared completions log (keyed hotel + data date)."""
    if scope is None or scope.empty:
        return
    dates = sorted([d for d in df["file_date"].dropna().astype(str).unique() if d], reverse=True)
    if not dates:
        return
    sc = scope.dropna(subset=["giata"]).drop_duplicates("giata").copy()
    if sc.empty:
        return
    with st.expander(label):
        dd = st.selectbox("Pricing data date", dates, index=0, key=f"{key_prefix}_pe_date")
        existing = (comp_log[comp_log["data_date"].astype(str) == str(dd)]
                    if (comp_log is not None and not comp_log.empty) else pd.DataFrame())
        ex = {str(r["giata"]): r for _, r in existing.iterrows()} if not existing.empty else {}
        gids = sc["giata"].astype(str).tolist()
        grid = pd.DataFrame({
            "Giata": gids,
            "Hotel": sc["hotel_name"].astype(str).values,
            "Dest": sc["destination"].astype(str).values,
            "Suggested Δ": sc["suggested"].astype(str).values,
            "✅ Done": [bool(ex.get(g, {}).get("done", 0)) for g in gids],
            "Initials": [str(ex.get(g, {}).get("initials", "") or "") for g in gids],
            "Note": [str(ex.get(g, {}).get("note", "") or "") for g in gids],
        })
        edited = st.data_editor(
            grid, hide_index=True, use_container_width=True,
            height=min(430, 80 + 35 * max(len(grid), 1)), key=f"{key_prefix}_pe_editor",
            column_config={
                "Giata": st.column_config.TextColumn(disabled=True),
                "Hotel": st.column_config.TextColumn(disabled=True),
                "Dest": st.column_config.TextColumn(disabled=True),
                "Suggested Δ": st.column_config.TextColumn("Suggested Δ", disabled=True),
                "✅ Done": st.column_config.CheckboxColumn("✅ Done"),
                "Initials": st.column_config.TextColumn("Initials", max_chars=8),
                "Note": st.column_config.TextColumn("Note — what you changed", max_chars=300),
            })
        if st.button("💾 Save progress", key=f"{key_prefix}_pe_save"):
            smap = dict(zip(gids, sc["suggested"].astype(str)))
            rows = []
            for _, er in edited.iterrows():
                g = str(er["Giata"]); done = bool(er["✅ Done"])
                note = str(er.get("Note", "") or "").strip()
                initials = str(er.get("Initials", "") or "").strip()
                if done or note or initials:
                    rows.append({"giata": g, "hotel_name": er["Hotel"], "destination": er["Dest"],
                                 "data_date": str(dd), "done": int(done),
                                 "suggested_action": smap.get(g, ""), "note": note, "initials": initials})
            saved = save_completions(rows)
            st.success(f"Saved {saved} update(s) for data date {dd}. ✅ flags update on next interaction.")
df = match_clicks(df, clicks_df)
df = attach_bookings(df, bookings_df)
if not df.empty:
    df["offer"] = df["giata"].astype(str).map(offer_status).fillna("")

# Booking totals by destination (matched to the comparison destinations in view)
def dest_booking_totals(comp_df, bk_df):
    """Bookings/revenue per comparison destination, joined by GIATA so it's robust to
    destination-name differences (e.g. comparison 'CUN' vs booking 'Cancun')."""
    if bk_df is None or bk_df.empty or comp_df.empty:
        return pd.DataFrame(columns=["destination", "bookings", "revenue"])
    giata_to_dest = (comp_df.dropna(subset=["giata"])
                     .drop_duplicates("giata").set_index(comp_df.dropna(subset=["giata"])
                     .drop_duplicates("giata")["giata"].astype(str))["destination"].to_dict())
    b = bk_df.copy()
    b["giata"] = b["giata"].astype(str)
    b["destination"] = b["giata"].map(giata_to_dest)
    b = b[b["destination"].notna()]
    if b.empty:
        return pd.DataFrame(columns=["destination", "bookings", "revenue"])
    return b.groupby("destination", as_index=False).agg(
        bookings=("bookings", "sum"), revenue=("revenue", "sum"))

dest_bk = dest_booking_totals(df, bookings_df)

st.title("🏨 D2 Trivago — Hotel-Only Pricing × Clicks Engine")
if df.empty:
    st.info("👆 Upload a Trivago comparison file (and the Clicks & Costs Master) in the sidebar to begin.")
    st.stop()

hotels = aggregate_hotels(df)
hotels = attach_rule_levers(hotels, rules_df)
if not hotels.empty:
    hotels["offer"] = hotels["giata"].astype(str).map(offer_status).fillna("")

_has_bk = bookings_df is not None and not bookings_df.empty
_bk_matched = int((hotels["bk_bookings"] > 0).sum()) if (not hotels.empty and "bk_bookings" in hotels) else 0
if _has_bk and not hotels.empty:
    _bk_total = int(hotels["bk_bookings"].sum())
    _bk_caption = f"{_bk_total:,} bookings ({_bk_matched} hotels matched)"
else:
    _bk_caption = "no bookings loaded"
_clk_caption = "clicks joined" if (clicks_df is not None and not clicks_df.empty) else "no clicks loaded"
st.caption(f"Destinations: **{', '.join(sorted(df['destination'].unique()))}** · "
           f"{df['hotel_name'].nunique()} hotels · {len(df):,} comparison rows · "
           f"{_clk_caption} · {_bk_caption}")

st.markdown(
    "<div style='display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:0.82rem;margin:-4px 0 6px 0;'>"
    "<span style='opacity:0.7'>Colour key:</span>"
    "<span style='background:rgba(22,160,90,0.40);padding:2px 10px;border-radius:4px;'>Win / raise margin</span>"
    "<span style='background:rgba(205,55,45,0.40);padding:2px 10px;border-radius:4px;'>Lose / cut margin</span>"
    "<span style='background:rgba(232,150,28,0.40);padding:2px 10px;border-radius:4px;'>Hold / no comp</span>"
    "<span style='background:rgba(38,120,210,0.40);padding:2px 10px;border-radius:4px;'>Product/cost fix · standard offer</span>"
    "<span style='background:rgba(150,80,210,0.42);padding:2px 10px;border-radius:4px;'>🌟 Exclusive offer</span>"
    "</div>", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════
GUIDE_MD = r"""
# 📖 How this dashboard works — a guide for new users

This tool helps decide **what margin/markup to run on each hotel** by combining four
things: how our **price** compares to competitors on Trivago, how many **clicks** a hotel
gets, how many **bookings** it actually takes, and the **markup rules** that set the price.
It is **hotel-only** — no flights or packages anywhere in the logic.

Every suggestion is a **starting point for a human to approve**, never an automatic change.

---

## 1. The four data inputs (loaded in the sidebar)

| File | What it gives | How it links to a hotel |
|---|---|---|
| **Trivago comparison** (`Results ALL`) | Per search: our price vs the lead-in competitor, Win/Lose, our current margin, and the "parity margin" | Hotel name + **Giata** + destination |
| **Clicks & Costs master** | Per hotel: Trivago clicks, cost, CPC | Hotel **name** match within destination (fuzzy, ≥78% similarity) |
| **Booking export** | Per hotel: actual bookings, revenue, profit | **Giata** (exact match — most reliable) |
| **Markup rule engine** | Every pricing rule (*If conditions Then markup*) | **Giata** inside each rule's conditions |

Everything joins on the hotel's **Giata id**. A hotel only appears with clicks/bookings/rules
if it was found in those files; otherwise those columns simply show 0 or blank.

---

## 2. The core ideas behind every number

**Result of a search** (from the comparison file):
- **Win** = our price is cheaper than the lead-in competitor.
- **Lose** = our price is dearer.
- **Win – No Comp** = we have a price but no competitor showed up to compare.
- **No D2 Result** = a competitor showed but **we had no price/availability** (a product/availability gap).

**Win rate** = Wins ÷ (Wins + Loses) across all of a hotel's searches and dates.

**Margin, and the key trick — "parity margin".** The comparison file gives two margins per
search: our **current margin**, and a **"New margin"** which is the margin we'd be running
*if our price exactly equalled the competitor's price*. That second number is the hotel's
**parity margin** — the break-even point. The insight that drives the whole engine:

> We **win** a search whenever our margin sits **below** its parity margin (we're cheaper),
> and **lose** when our margin sits **above** it (we're dearer).

So parity margin is the bridge between "price competitiveness" and "margin" — it lets the app
work out what a margin change would do to wins.

**Margin guardrails** used throughout:
- **Floor = 7%** — never suggest pricing below this.
- **Near = 8.5%** — caution band just above the floor.
- **Ceiling = 15%** — margins above this are flagged as having strong headroom.

**Click level** (per hotel, relative to others in the same destination): **high / mid / low**,
based on where the hotel sits in that destination's click distribution.

**Demand score** = a blend of **bookings (60%)** and **clicks (40%)**, used to rank hotels so
the highest-impact actions come first. Real bookings lead; if no booking file is loaded it
falls back to metasearch bookings.

---

## 3. The Decision Matrix — how each hotel is classified

Each hotel is placed into an action group from its **price stance** (winning/losing on price),
its **click level**, and its **margin headroom**:

| Group | Situation | Recommended action |
|---|---|---|
| **Q2 · Raise Margin** | Winning on price + high clicks + room below ceiling | **Raise** the margin to capture profit — demand is strong and we're cheap |
| **Q3 · Reduce Price** | Losing + low clicks + a cut still stays above the 7% floor | **Reduce** margin to become competitive — low risk since we weren't getting clicks anyway |
| **Q1 · Visibility Gap** | Winning on price but **not** getting clicks | **Not a price problem** — investigate content/photos/position. Do *not* cut price |
| **Q4 · Product/Cost Fix** | A flagged product gap, or losing with low clicks and no room to cut | Can't win profitably on price — **renegotiate cost / contracting**, don't cut |
| Reduce to Convert | Losing but still getting clicks, with margin room | Reduce to convert the demand we're already getting |
| Cost Fix (high demand) | Losing + high clicks + no margin room | High demand but we can't win on price — fix the cost base |
| Maintain / Hold | Winning, mid clicks, or at the ceiling | Hold and monitor |

---

## 4. The margin suggestion engine (the "Margin Moves" logic)

For each hotel the app suggests **one markup change** to run across all its dates. It uses only
**Win/Lose** rows (Win-No-Comp excluded), the **latest run** if a date was compared more than
once, and models **price sensitivity**: changing markup by Δ percentage points shifts a search's
gap by Δ × 100 ÷ (100 + markup). A loss within the **noise band (0.5%)** of break-even is treated
as a draw and never chased, so "winning" means gap > −0.5%.

It then follows one of two paths:

1. **Winning comfortably (win rate ≥ 70%) → raise to capture margin.** It takes the *largest*
   raise that keeps the projected win rate ≥ 70% and flips no more than ~10% of solid wins into
   real losses. The point: when we're winning well, push margin up rather than shaving it to
   chase the last marginal win.
2. **Contested (win rate < 70%) → balanced.** It tests every small change and picks the one with
   the highest **expected booked margin = projected win rate × projected markup**. This cuts to
   flip losses when worthwhile, holds when already best, and — if losing badly — leaves it for the
   Product/Cost view. Among near-ties it chooses the **smallest move** (gives away the least margin).

**Guardrails:** never push any search's markup below the **7% floor** or above the **15%
ceiling**, and never move more than **±5 percentage points** in one pass.

The table shows **WR Now → WR Est** (the noise-tolerant win rate now and after the move). The
change is in **pp (percentage points)** — the gap between two markup percentages — and
**Cur Margin + Change = Suggested** always reconciles.

> The suggested value is a close **steer, not an exact target**, because of how system and
> consumer markups layer together.

---

## 4b. Product-gap flagging (when price can't fix it)

A **product gap** is a hotel where we lose, but matching the competitor would need a markup
**below the 7% floor** — i.e. we'd have to sell below our minimum margin to compete. That's
unreachable on price, so it's a **product/contracting** problem, never a price cut.

For each losing search the app looks at the **markup-to-match** (the markup we'd be left on after
dropping to the competitor's price; taken from the comparison sheet, or derived as
`current markup − |gap| × (1 + current markup/100)`). A loss is "below floor" when its
markup-to-match is between 0 and 7%. A hotel is flagged **🚫 Product Gap** when **at least half**
its losses are below floor **and** there are **at least 2** such losses. These are surfaced in the
Product/Cost Fix tab, sorted to the top with booking volume.

---

## 5. Turning a suggestion into a real rule change (the rule engine)

When the rule export is loaded, each suggested move is matched to the **exact markup rule** to
edit. Only **hotel-only, enabled, consumer markups** are considered (Manual or Exclusive
layers); flight, package, package-only, and system-cost rules are excluded as levers.

For a hotel that needs a change, the app:
1. Finds enabled consumer rules whose Giata condition **includes that hotel**.
2. Splits them into:
   - **Single-hotel rules** → **safe to EDIT** (the change affects only this hotel). It picks
     the cleanest one (Exclusive preferred, then fewest extra conditions) and proposes
     `current value + the move`.
   - **Shared rules** (one rule listing several hotels) → **never edited** — changing them
     would move every hotel on the list. They're shown as context with a "do not edit" warning.
3. If no single-hotel rule exists → it proposes **creating a new single-hotel rule**, which
   stacks on top additively and adjusts only this hotel.

System-cost rules are shown for context only and are **never** changed to chase win rate.

---

## 6. The colour key (used in every table)

- 🟩 **Green** = win / raise margin / healthy win rate (≥70%) / positive change.
- 🟥 **Red** = lose / cut margin / low win rate (<50%) / product gap.
- 🟨 **Amber** = hold / no competitor / win rate 50–69%.
- 🟦 **Blue** = product/cost/visibility fix · a standard (non-exclusive) offer.
- 🟪 **Purple** = a live **exclusive** offer.

---

## 6b. Hotel offers

Upload an offers export (Giata, Hotel Name, Hermes Destination, Offer Type, Is Exclusive,
Deal Status, Booking From/To, Travel From/To, details). A row is a **live offer** when the deal
status is Active (or blank), today sits inside the booking window, and the travel-to date is still
ahead. Each hotel is then summarised to one **Offer** status — **🌟 Exclusive** (any live exclusive),
**Offer** (any live standard), or blank — shown as a colour-coded column on the hotel tables
(purple = exclusive, blue = standard). Live status is recomputed against today's date each run, and
hotels with a live exclusive offer are folded into the Margin Moves top list so they aren't missed.

---

## 7. What each tab shows

- **⚡ Decision Matrix** — every hotel placed into its action group, with a scatter of price
  competitiveness vs clicks, plus an Offer flag. The big-picture "where do I focus" view.
- **🎯 Margin Moves** — the headline output: the single suggested margin per hotel, ranked by
  booking + click demand. The **Rule** column shows the exact rule text to edit (or a new
  single-hotel rule to create); the **Offer** column flags live offers.
- **Overview** — totals and trends: win rate by departure window and destination, bookings by
  destination, and which competitors lead on Trivago.
- **Hotel Actions** — the raw search-level detail (every date/board/competitor) behind a hotel.
- **↑ Raise Margin** — hotels in Q2: winning with demand and headroom.
- **↓ Reduce Price** — hotels in Q3: losing with low clicks and room to cut.
- **👁 Visibility Gap** — hotels in Q1: cheap but not getting clicks (a content/position fix).
- **🔧 Product/Cost Fix** — hotels flagged as **product gaps** (losses unreachable on price,
  below the floor), with the below-floor breakdown and bookings at risk.
- **No Comp** — searches with no competitor (price to destination norms) and availability gaps.
- **Suppliers** — performance by supplier. Supplier codes after the dash are **merged to the
  parent brand** (e.g. `OTS - AMXCUN75FI` → `OTS`), so you see win rate, margin and bookings
  rolled up per supplier.
- **↗ Price & Clicks Trends** — the **By Travel Month & Destination** headline chart (grouped
  columns per destination + up to two per-destination overlay lines, each on its own axis, with
  metric selectors and a **focused view** that adds run-date/window/month-range filters and a
  **year-on-year** toggle); price vs competitor over departure dates; **bookings by month** (booked
  vs departure/travel); **win rate · avg markup · bookings** by departure month; **booking pace by
  booked month**; and top hotels by clicks and bookings. Win rate and markup are shown to 2 dp.
- **📂 Files** — what's loaded, with CSV exports of the enriched data and the decision matrix.
- **✅ Progress** — shared completion tracking (see §7c).

---

## 7c. Tracking what's done (the ✅ Progress tab)

In the **✅ Progress** tab you pick the **pricing-data date** you're actioning against, then see the
in-scope hotels (filtered by destination, with departure-window scope following the sidebar) in an
editable grid alongside their suggested Δ. Tick **✅ Done**, add your **initials** and a **note** of
what you changed, and hit **Save progress**. It writes to a shared log in the app database, so anyone
else viewing the app sees the same state.

Completion is keyed by **hotel + data date** and captures the suggested action and your note at the
time. You don't have to switch to this tab to log — **every hotel table (Margin Moves, Decision
Matrix, Raise, Reduce, Visibility, Product/Cost, Hotel Actions, No Comp) has a collapsible
"✅ Log progress for these hotels" editor right beneath it**, so you can tick things off as you work
through that tab; it writes to the same shared log against the latest pricing-data date. Every hotel
table also shows a green-tinted **✅ Done** column with the most recent data date a hotel was
actioned, so at a glance you can see what's already handled and avoid duplicating it. When
a newer pricing file is loaded, the previous action against the earlier date stays in the log — the
**Action Log** at the bottom lists everything (data date, hotel, action taken, note, who, timestamp),
the foundation for tracking prior actions and their impact as data accumulates. You can export it to
CSV, and there's a separate guarded "clear log" button.

Two caveats:
- The log lives in the app's database, shared across people viewing at the same time and persistent
  across normal sessions — **but Streamlit Cloud storage resets when the app is redeployed or
  restarts.** It's reliable for day-to-day "don't double-up" coordination; **export the action log
  periodically** if you're building a long-run history. The durable next step is to point the log at
  an external database (e.g. a small hosted Postgres) so it survives redeploys.
- The action log is deliberately **left out of "Clear ALL data"**, so re-uploading pricing files
  never wipes your history; clearing it is a separate explicit button in the Progress tab.

---

## 7b. Data captured for the future

Bookings are now stored at **month level** by both *booked* date and *departure/travel* date
(with revenue and margin), and every run is kept. As more runs and history accumulate this is the
foundation for **year-on-year comparisons**, **measuring whether a markup change improved the
outcome**, and **predictive insight** into pricing, margin and competitor movement over time.

---

## 8. Important cautions

- **Suggestions are starting points**, for a human to approve — nothing is applied automatically.
- **Margin changes are in percentage points (pp)**, the gap between two margin percentages —
  not a relative % change.
- The **new rule value is approximate** (markup layering), good as a steer.
- **Never touch system-cost or shared rules** to fix one hotel.
- **Watch date-gated rules** — some only apply within a travel window; flagged where present.
- Coverage depends on the data: hotels match by Giata (bookings/rules) or fuzzy name (clicks),
  and unmatched hotels simply show blanks rather than breaking anything.

---

## 9. Access (admin note)

The app is password-protected. The password is **not** stored in the code — it's read from the
app's **Settings → Secrets** (`st.secrets`). On Streamlit Cloud, open the app's ⋯ menu →
**Settings → Secrets** and add either `password = "yourpassword"` or a section
`[auth]` with `password = "yourpassword"`; locally, put the same in `.streamlit/secrets.toml`.
If no password is set, the app stays open (it never hard-locks) and shows how to configure one.
"""

tabs = st.tabs(["⚡ Decision Matrix", "🎯 Margin Moves", "Overview", "Hotel Actions",
                "↑ Raise Margin", "↓ Reduce Price", "👁 Visibility Gap",
                "🔧 Product/Cost Fix", "No Comp", "Suppliers",
                "↗ Price & Clicks Trends", "📂 Files", "✅ Progress", "📖 Guide"])


def fmt_move(r):
    try:
        return f"{r['current_margin']:.1f}% → {r['sug_margin']:.1f}% ({r['sug_delta']:+.1f}pp)"
    except Exception:
        return "—"

# Active pricing-data date for in-place logging (latest loaded), and a giata→suggested-move map
_dates_avail = sorted([d for d in df["file_date"].dropna().astype(str).unique() if d], reverse=True) \
    if (not df.empty and "file_date" in df) else []
active_data_date = _dates_avail[0] if _dates_avail else ""
try:
    sug_move_map = dict(zip(hotels["giata"].astype(str), hotels.apply(fmt_move, axis=1))) \
        if not hotels.empty else {}
except Exception:
    sug_move_map = {}

def inline_progress(scope_df, key):
    """Collapsible in-place completion logger for the hotels shown in a tab — writes to the
    same shared log as the Progress tab, against the latest pricing-data date."""
    if scope_df is None or scope_df.empty or not active_data_date:
        return
    s = scope_df.dropna(subset=["giata"]).drop_duplicates("giata").copy()
    if s.empty:
        return
    with st.expander(f"✅ Log progress for these hotels (data date {active_data_date})"):
        gids = s["giata"].astype(str).tolist()
        existing = comp_log[comp_log["data_date"].astype(str) == str(active_data_date)] \
            if (comp_log is not None and not comp_log.empty) else pd.DataFrame()
        ex_map = {str(r["giata"]): r for _, r in existing.iterrows()} if not existing.empty else {}
        grid = pd.DataFrame({
            "Giata": gids,
            "Hotel": s["hotel_name"].values,
            "Dest": s["destination"].values,
            "Suggested Δ": [sug_move_map.get(g, "") for g in gids],
            "✅ Done": [bool(ex_map.get(g, {}).get("done", 0)) for g in gids],
            "Initials": [str(ex_map.get(g, {}).get("initials", "") or "") for g in gids],
            "Note": [str(ex_map.get(g, {}).get("note", "") or "") for g in gids],
        })
        edited = st.data_editor(
            grid, hide_index=True, use_container_width=True, height=min(420, 80 + 35 * len(grid)),
            key=f"{key}_editor", column_config={
                "Giata": st.column_config.TextColumn(disabled=True),
                "Hotel": st.column_config.TextColumn(disabled=True),
                "Dest": st.column_config.TextColumn(disabled=True),
                "Suggested Δ": st.column_config.TextColumn("Suggested Δ", disabled=True),
                "✅ Done": st.column_config.CheckboxColumn("✅ Done"),
                "Initials": st.column_config.TextColumn("Initials", max_chars=8),
                "Note": st.column_config.TextColumn("Note — what you changed", max_chars=300),
            })
        if st.button("💾 Save progress", key=f"{key}_save"):
            rows = []
            for _, er in edited.iterrows():
                g = str(er["Giata"])
                done = bool(er["✅ Done"])
                note = str(er.get("Note", "") or "").strip()
                initials = str(er.get("Initials", "") or "").strip()
                if done or note or initials:
                    rows.append({"giata": g, "hotel_name": er["Hotel"], "destination": er["Dest"],
                                 "data_date": str(active_data_date), "done": int(done),
                                 "suggested_action": sug_move_map.get(g, ""),
                                 "note": note, "initials": initials})
            saved = save_completions(rows)
            st.success(f"Saved {saved} update(s) against {active_data_date}. "
                       f"Reopen the tab to see the ✅ Done flags refresh.")

# ── Traffic-light colour scheme (semi-transparent → readable on light & dark) ────
CLR_GREEN  = "background-color: rgba(22,160,90,0.40)"    # win / raise margin
CLR_RED    = "background-color: rgba(205,55,45,0.40)"    # lose / cut margin / product gap
CLR_ORANGE = "background-color: rgba(232,150,28,0.40)"   # amber: hold / no comp / unsure
CLR_AMBER  = CLR_ORANGE                                  # alias (brief calls it amber)
CLR_BLUE   = "background-color: rgba(38,120,210,0.40)"   # product/cost/visibility fix · standard offer
CLR_PURPLE = "background-color: rgba(150,80,210,0.42)"   # exclusive offer
CLR_NONE   = ""

def _num(v):
    try:
        return float(str(v).replace("%", "").replace("pp", "").replace("+", "")
                     .replace("£", "").replace(",", "").strip())
    except Exception:
        return None

def c_result(v):
    s = str(v).lower()
    if s == "win":
        return CLR_GREEN
    if s == "lose":
        return CLR_RED
    if "no comp" in s or "no d2" in s:
        return CLR_AMBER
    return CLR_NONE

def c_signed(v):
    n = _num(v)
    if n is None:
        return CLR_NONE
    if n > 0.05:
        return CLR_GREEN
    if n < -0.05:
        return CLR_RED
    return CLR_AMBER

def c_gap(v):
    n = _num(v)
    if n is None:
        return CLR_NONE
    if n > 0:
        return CLR_GREEN      # we're cheaper → winning
    if n < 0:
        return CLR_RED        # we're dearer → losing
    return CLR_AMBER

def c_winrate(v):
    """Win-rate cell: green ≥70, amber 50–69, red <50 (per the brief)."""
    n = _num(v)
    if n is None:
        return CLR_NONE
    if n <= 1.0:
        n *= 100              # fraction → %
    if n >= 70:
        return CLR_GREEN
    if n >= 50:
        return CLR_AMBER
    return CLR_RED

def c_action(v):
    s = str(v).lower()
    if "product" in s or "cost" in s or "visibility" in s or "gap" in s or "fix" in s:
        return CLR_BLUE
    if "raise" in s or "headroom" in s or "capture" in s or "advantage" in s or "increase" in s:
        return CLR_GREEN
    if "cut" in s or "reduce" in s or "flip" in s or "suppress" in s:
        return CLR_RED
    if ("hold" in s or "maintain" in s or "monitor" in s or "review" in s
            or "insufficient" in s or "no comp" in s or "guided" in s or "optimal" in s):
        return CLR_AMBER
    return CLR_NONE

def c_offer(v):
    s = str(v).lower()
    if "exclusive" in s or "🌟" in str(v):
        return CLR_PURPLE
    if "offer" in s:
        return CLR_BLUE
    return CLR_NONE

def c_done(v):
    """Green tint when a hotel has been actioned (a data date is present)."""
    s = str(v).strip()
    return CLR_GREEN if (s and s.lower() not in ("nan", "none")) else CLR_NONE

def c_quadrant(v):
    s = str(v)
    if "Raise" in s:
        return CLR_GREEN
    if "Reduce" in s:
        return CLR_RED
    if "Q4" in s or "Cost" in s or "Product" in s or "Visibility" in s:
        return CLR_BLUE
    if "Hold" in s or "Maintain" in s or "Review" in s or "No Comp" in s:
        return CLR_AMBER
    return CLR_NONE

def c_move(v):
    """Colour a 'cur% → sug% (+/-Δpp)' string by the sign of the delta."""
    m = re.search(r"\(([+-]?\d+(?:\.\d+)?)pp\)", str(v))
    if not m:
        return CLR_NONE
    d = float(m.group(1))
    if d > 0.05:
        return CLR_GREEN
    if d < -0.05:
        return CLR_RED
    return CLR_AMBER

def _const(color):
    return lambda v: color

def apply_colors(styler, mapping, cols):
    """Apply per-column cell colour functions where the column exists (pandas 2.x/3.x)."""
    use_map = hasattr(styler, "map")
    for col, fn in mapping.items():
        if col in cols:
            try:
                styler = styler.map(fn, subset=[col]) if use_map else styler.applymap(fn, subset=[col])
            except Exception:
                pass
    return styler

# One reusable styler for per-hotel tables (action queues, hotel actions, etc.)
STYLE_MAP = {
    "Action": c_action, "Recommended Action": c_action, "Note": c_action, "Stance": c_action,
    "Price Stance": c_action, "Verdict": _const(CLR_RED),
    "Result": c_result, "Offer": c_offer,
    "✅ Done": c_done,
    "Quadrant": c_quadrant,
    "Change": c_signed, "Sugg. Markup Δ": c_signed, "Rule change": c_signed,
    "Gap %": c_gap, "Avg Gap %": c_gap,
    "Win Rate": c_winrate, "WR Now": c_winrate, "WR Est": c_winrate, "Win Rate %": c_winrate,
    "Margin (cur→suggested)": c_move, "Margin Move": c_move,
}

def style_table(df, fmt=None, extra=None):
    """Return a Styler with the standard colour scheme applied to known columns by name.
    Tolerates string-formatted cells; unknown columns are skipped."""
    sty = df.style.format(fmt) if fmt else df.style
    mapping = dict(STYLE_MAP)
    if extra:
        mapping.update(extra)
    return apply_colors(sty, mapping, df.columns)

# ── "By Travel Month & Destination" multi-axis chart (brief §2a/§2b) ─────────────
_DEST_PALETTE = ["#1B6FD4", "#0A7C4E", "#D4AF37", "#C0392B", "#8E44AD", "#16A085",
                 "#E67E22", "#2C3E50"]
_METRIC_OPTS = ["Bookings", "Win Rate %", "Avg Markup %"]

def _month_label(m):
    try:
        return pd.to_datetime(str(m) + "-01").strftime("%b %Y")
    except Exception:
        return str(m)

def tmd_aggregate(comp_df, bmonths, yoy=False):
    """Long table [destination, month, year, WinRate, Markup, Bookings] per
    (destination, departure month). Win rate uses real comparisons only."""
    cols = ["destination", "month", "year", "WinRate", "Markup", "Bookings"]
    if comp_df is None or comp_df.empty:
        return pd.DataFrame(columns=cols)
    c = comp_df[comp_df["dep_month"].astype(str) != ""].copy()
    if c.empty:
        return pd.DataFrame(columns=cols)
    # win rate + markup per (dest, dep_month)
    recs = []
    for (dest, m), g in c.groupby(["destination", "dep_month"]):
        comp = g[g["result"].isin(["Win", "Lose"])]
        wr = round((comp["result"] == "Win").sum() / len(comp) * 100, 2) if len(comp) else np.nan
        mk = g[g["current_margin"] > 0]["current_margin"]
        recs.append({"destination": dest, "month": m,
                     "WinRate": wr,
                     "Markup": round(float(mk.mean()), 2) if len(mk) else np.nan})
    out = pd.DataFrame(recs)
    # bookings per (dest, dep_month) via giata → dest map
    g2d = (c.dropna(subset=["giata"]).drop_duplicates("giata")
           .assign(giata=lambda d: d["giata"].astype(str)).set_index("giata")["destination"].to_dict())
    if bmonths is not None and not bmonths.empty:
        bm = bmonths[bmonths["dep_month"] != ""].copy()
        bm["destination"] = bm["giata"].astype(str).map(g2d)
        bm = bm[bm["destination"].notna()]
        bk = bm.groupby(["destination", "dep_month"], as_index=False)["bookings"].sum()
        bk.columns = ["destination", "month", "Bookings"]
        out = out.merge(bk, on=["destination", "month"], how="left")
    else:
        out["Bookings"] = np.nan
    out["Bookings"] = out["Bookings"].fillna(0)
    out["year"] = out["month"].str.slice(0, 4)
    return out

def build_tmd_fig(longdf, cols_metric, line1, line2, yoy=False):
    """Grouped bars per destination (or per year in YoY) + up to two per-series overlay
    lines, each metric on its own y-axis. Returns (fig, note)."""
    note = ""
    if longdf is None or longdf.empty:
        return None, "No travel-month data for this selection."
    metric_key = {"Bookings": "Bookings", "Win Rate %": "WinRate", "Avg Markup %": "Markup"}
    series_col = "year" if yoy else "destination"

    d = longdf.copy()
    if yoy:
        # x = month-of-year (Jan..Dec); series = departure year
        d["mo"] = d["month"].str.slice(5, 7)
        d = d.groupby(["year", "mo"], as_index=False).agg(
            Bookings=("Bookings", "sum"), WinRate=("WinRate", "mean"), Markup=("Markup", "mean"))
        d["WinRate"] = d["WinRate"].round(2); d["Markup"] = d["Markup"].round(2)
        order = [f"{i:02d}" for i in range(1, 13)]
        x_vals = [o for o in order if o in set(d["mo"])]
        x_labels = [pd.to_datetime("2000-" + o + "-01").strftime("%b") for o in x_vals]
        xkey = "mo"
        if d["year"].nunique() <= 1:
            note = "Only one departure year loaded — this becomes a true year-on-year view once more years accumulate."
    else:
        months = sorted(set(d["month"]))
        x_vals = months
        x_labels = [_month_label(m) for m in months]
        xkey = "month"

    series_vals = list(dict.fromkeys(d[series_col].tolist()))
    # cap destinations (not years) for readability
    if not yoy and len(series_vals) > 8:
        tot = d.groupby("destination")["Bookings"].sum().sort_values(ascending=False)
        series_vals = list(tot.head(8).index)
        note = f"Showing the top 8 destinations by bookings (of {d['destination'].nunique()})."

    col_m = metric_key[cols_metric]
    fig = go.Figure()
    n_right = (1 if line1 and line1 != "None" else 0) + (1 if line2 and line2 != "None" else 0)

    for i, sv in enumerate(series_vals):
        colour = _DEST_PALETTE[i % len(_DEST_PALETTE)]
        sd = d[d[series_col] == sv].set_index(xkey)
        ybar = [sd[col_m].get(x, None) if x in sd.index else None for x in x_vals]
        fig.add_trace(go.Bar(x=x_labels, y=ybar, name=str(sv), legendgroup=str(sv),
                             marker_color=colour))
        for li, (lm, axis, dash) in enumerate([(line1, "y2", "dash"), (line2, "y3", "dot")]):
            if lm and lm != "None":
                lk = metric_key[lm]
                yline = [sd[lk].get(x, None) if x in sd.index else None for x in x_vals]
                fig.add_trace(go.Scatter(x=x_labels, y=yline, name=f"{sv} · {lm}",
                                         legendgroup=str(sv), showlegend=False, yaxis=axis,
                                         mode="lines+markers", line=dict(color=colour, dash=dash, width=2)))

    layout = dict(barmode="group", height=430, margin=dict(t=50, b=10),
                  legend=dict(orientation="h", y=1.16),
                  yaxis=dict(title=cols_metric),
                  xaxis=dict(title=("Month of year" if yoy else "Departure / travel month")))
    # reference lines on the columns axis
    if cols_metric == "Win Rate %":
        fig.add_hline(y=50, line_dash="dot", line_color="#E04A3F")
        layout["yaxis"]["range"] = [0, 100]
    elif cols_metric == "Avg Markup %":
        fig.add_hline(y=MARGIN_FLOOR, line_dash="dot", line_color="#E04A3F")
        fig.add_hline(y=MARGIN_CEILING, line_dash="dot", line_color="#27AE60")
    # extra right-hand axes for the overlay lines
    if n_right >= 1:
        l1name = line1 if (line1 and line1 != "None") else line2
        layout["yaxis2"] = dict(title=l1name, overlaying="y", side="right", showgrid=False)
    if n_right == 2:
        layout["xaxis"]["domain"] = [0.0, 0.90]
        layout["yaxis3"] = dict(title=line2, overlaying="y", side="right",
                                anchor="free", position=1.0, showgrid=False)
    fig.update_layout(**layout)
    return fig, note

# ── 1. DECISION MATRIX ──────────────────────────────────────────────────────────
with tabs[0]:
    st.subheader("⚡ Clicks × Price Decision Matrix")
    st.caption("Each hotel placed by price competitiveness, margin headroom and click volume.")
    if hotels.empty:
        st.info("No hotel-level data with current filters.")
    else:
        q = hotels.copy()
        cnt = q["quadrant"].value_counts()
        cols = st.columns(4)
        cols[0].metric("↑ Raise Margin (Q2)", int(cnt.get("Q2 · Raise Margin", 0)))
        cols[1].metric("↓ Reduce Price (Q3)", int(cnt.get("Q3 · Reduce Price", 0)))
        cols[2].metric("👁 Visibility Gap (Q1)", int(cnt.get("Q1 · Visibility Gap", 0)))
        cols[3].metric("🔧 Product/Cost (Q4)", int(cnt.get("Q4 · Product/Cost Fix", 0)))

        sc = q[q["price_stance"].isin(["Winning", "Losing", "Level"])].copy()
        if not sc.empty:
            fig = px.scatter(
                sc, x="avg_diff_pct", y="clicks", color="quadrant", size="searches",
                hover_name="hotel_name",
                hover_data={"giata": True, "bk_bookings": ":.0f", "current_margin": ":.1f",
                            "sug_margin": ":.1f", "win_rate": ":.0%", "destination": True},
                labels={"avg_diff_pct": "Avg price gap vs competitor % (→ cheaper/winning)",
                        "clicks": "Trivago clicks", "bk_bookings": "Bookings"},
                title="Price competitiveness vs click volume")
            fig.add_vline(x=0, line_dash="dot", line_color="#888")
            fig.update_layout(height=460, legend=dict(orientation="h", y=-0.25))
            st.plotly_chart(fig, use_container_width=True, key="dm_scatter")

        st.markdown("#### Ranked Actions")
        q["_ord"] = q["quadrant"].apply(lambda x: QUADRANT_ORDER.index(x) if x in QUADRANT_ORDER else 99)
        q = q.sort_values(["_ord", "bk_bookings", "clicks", "searches"],
                          ascending=[True, False, False, False])
        q["Margin Move"] = q.apply(fmt_move, axis=1)
        _dm_off = "offer" in q.columns and q["offer"].astype(str).str.len().gt(0).any()
        _dmc = ["giata", "hotel_name", "destination", "quadrant", "decision",
                "click_level", "clicks", "bk_bookings", "avg_diff_pct",
                "sug_cur_wr", "sug_pred_wr", "Margin Move", "searches"]
        _dmn = ["Giata", "Hotel", "Dest", "Quadrant", "Recommended Action",
                "Click Lvl", "Clicks", "Bookings", "Avg Gap %",
                "WR Now", "WR Est", "Margin (cur→suggested)", "Searches"]
        if _dm_off:
            _dmc.insert(5, "offer"); _dmn.insert(5, "Offer")
        show = q[_dmc].copy()
        show.columns = _dmn
        show = with_done(show)
        _sty = show.style.format({"Clicks": "{:.0f}", "Bookings": "{:.0f}",
                                  "Avg Gap %": "{:.1f}%", "WR Now": "{:.0%}",
                                  "WR Est": "{:.0%}"})
        _sty = apply_colors(_sty, {"Quadrant": c_quadrant, "Recommended Action": c_action,
                                   "Avg Gap %": c_gap, "WR Now": c_winrate, "WR Est": c_winrate,
                                   "Offer": c_offer, "✅ Done": c_done,
                                   "Margin (cur→suggested)": c_move}, show.columns)
        st.dataframe(_sty, use_container_width=True, height=520)
        inline_progress(q, "dm_prog")
with tabs[1]:
    st.subheader("🎯 Margin Moves — Quick Changes Across the Board")
    st.caption("One suggested margin level per hotel, applied across all its dates & boards. "
               "Ranked by booking + click demand so the highest-impact moves come first.")
    if hotels.empty:
        st.info("No hotel-level data with current filters.")
    else:
        st.info("**How the suggestion works:** using each search's price gap and a price-sensitivity "
                "model, the engine finds one markup level per hotel — it raises where you're winning "
                "comfortably (win rate ≥ 70%) and cuts to flip losses where you're contested, capped "
                "at the 7% floor, 15% ceiling and a ±5pp safe move. The **Rule** column shows the "
                "exact rule text to edit (or a new single-hotel rule to create). "
                "**WR Now → WR Est** shows the projected win-rate effect.")

        mv = hotels.copy()
        _has_rules = rules_df is not None and not rules_df.empty

        def _rule_label(r):
            """Show the rule's GeneratedText (the actual rule), not the id."""
            a = r.get("rule_action", "—")
            txt = str(r.get("rule_text", "") or "")
            if a in ("EDIT", "ADD NEW") and txt:
                return txt if len(txt) <= 180 else txt[:177] + "…"
            return "—"

        def _rule_delta(r):
            a = r.get("rule_action", "—")
            if a == "EDIT":
                if pd.notna(r.get("rule_new")) and pd.notna(r.get("rule_cur")):
                    return f"{r['rule_cur']:.1f}% → {r['rule_new']:.1f}%"
                return "£ rule — manual"
            if a == "ADD NEW":
                return f"{r['sug_delta']:+.1f}% (new single-hotel)"
            return "—"

        if _has_rules:
            mv["Rule"] = mv.apply(_rule_label, axis=1)
            mv["Rule change"] = mv.apply(_rule_delta, axis=1)

        top = mv.sort_values(["demand_score", "bk_bookings", "clicks"], ascending=False).head(20).copy()
        # fold in any hotel with a live exclusive offer (dedupe on giata)
        if "offer" in mv.columns:
            excl = mv[mv["offer"].astype(str).str.contains("Exclusive")]
            if not excl.empty:
                extra = excl[~excl["giata"].isin(top["giata"])]
                if not extra.empty:
                    top = pd.concat([top, extra], ignore_index=True)
        top["Margin Move"] = top.apply(fmt_move, axis=1)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Hotels to RAISE", int((mv["sug_delta"] >= 0.5).sum()))
        c2.metric("Hotels to REDUCE", int((mv["sug_delta"] <= -0.5).sum()))
        c3.metric("Avg suggested move", f"{mv[mv['sug_delta'].abs()>=0.5]['sug_delta'].mean():+.1f}pp"
                  if (mv['sug_delta'].abs() >= 0.5).any() else "—")
        if _has_rules:
            c4.metric("Have editable rule", int((mv["rule_action"] == "EDIT").sum()))
        else:
            c4.metric("Bookings (top 20)", f"{int(top['bk_bookings'].sum()):,}")

        _has_offers = "offer" in mv.columns and mv["offer"].astype(str).str.len().gt(0).any()
        _mvcols = ["giata", "hotel_name", "destination", "clicks", "bk_bookings", "bk_revenue",
                   "sug_cur_wr", "current_margin", "sug_margin", "sug_delta", "sug_pred_wr", "sug_note"]
        _mvnames = ["Giata", "Hotel", "Dest", "Clicks", "Bookings", "Revenue £", "WR Now",
                    "Cur Margin", "Suggested", "Change", "WR Est", "Action"]
        if _has_offers:
            _mvcols.insert(3, "offer"); _mvnames.insert(3, "Offer")
        if _has_rules:
            _mvcols += ["Rule", "Rule change"]
            _mvnames += ["Rule", "Rule change"]
        _mvfmt = {"Clicks": "{:.0f}", "Bookings": "{:.0f}", "Revenue £": "£{:,.0f}",
                  "WR Now": "{:.0%}", "WR Est": "{:.0%}", "Cur Margin": "{:.1f}%",
                  "Suggested": "{:.1f}%", "Change": "{:+.1f}pp"}
        _mvmap = {"WR Now": c_winrate, "WR Est": c_winrate, "Change": c_signed,
                  "Action": c_action, "Offer": c_offer, "✅ Done": c_done}
        if _has_rules:
            _mvmap["Rule change"] = lambda v: (CLR_ORANGE if "new" in str(v).lower()
                                               else CLR_GREEN if "→" in str(v) else CLR_NONE)

        _mvmap["✅ Done"] = c_done
        show = top[_mvcols].copy(); show.columns = _mvnames
        show = with_done(show)
        _sty = apply_colors(show.style.format(_mvfmt), _mvmap, show.columns)
        st.dataframe(_sty, use_container_width=True, height=560)
        inline_progress(top, "mm_prog")

        with st.expander("Show ALL hotels (full suggestion list)"):
            full = mv.sort_values("demand_score", ascending=False)[_mvcols].copy()
            full.columns = _mvnames
            full = with_done(full)
            _styf = apply_colors(full.style.format(_mvfmt), _mvmap, full.columns)
            st.dataframe(_styf, use_container_width=True, height=460)
            st.download_button("⬇ Download all margin suggestions (CSV)",
                               data=mv.sort_values("demand_score", ascending=False).to_csv(index=False),
                               file_name="trivago_margin_suggestions.csv", mime="text/csv")

        # ── Per-hotel rule lookup: the exact rule to edit / create ──────────────
        st.markdown("#### 🛠 Find the exact rule to change")
        if not _has_rules:
            st.caption("Upload the Markup Rule Engine export in the sidebar to map each "
                       "suggested margin move to the specific rule to edit.")
        else:
            movers = mv[mv["sug_delta"].abs() >= 0.5].sort_values(
                ["demand_score", "bk_bookings"], ascending=False)
            if movers.empty:
                st.caption("No hotels currently need a margin change.")
            else:
                labels = movers.apply(
                    lambda r: f"{r['hotel_name']} ({r['giata']}) · {r['sug_delta']:+.1f}pp", axis=1).tolist()
                pick = st.selectbox("Hotel needing a change", labels, key="rule_pick")
                row = movers.iloc[labels.index(pick)]
                cons_idx, sys_idx = build_rule_index(rules_df)
                lev = lever_for_hotel(cons_idx, sys_idx, row["giata"], row["sug_delta"])
                if lev["action"] == "EDIT":
                    st.success(f"**EDIT rule #{lev['rule_id']}** ({lev.get('layer','')} markup)")
                    if pd.notna(lev["new"]):
                        st.markdown(f"Proposed: **{lev['cur']:.1f}% → {lev['new']:.1f}%** "
                                    f"(applies the {row['sug_delta']:+.1f}pp move)")
                    else:
                        st.markdown("This is a **fixed £** rule — adjust the amount manually.")
                    st.code(lev["text"], language="text")
                elif lev["action"] == "ADD NEW":
                    st.warning("**No editable single-hotel rule exists — create a new one** "
                               "(it stacks additively and touches only this hotel):")
                    st.code(lev["text"], language="text")
                if lev["shared"]:
                    st.markdown(f"⚠️ **{len(lev['shared'])} shared rule(s) — do NOT edit** "
                                f"(each moves several hotels at once):")
                    for s in lev["shared"][:5]:
                        st.caption(f"#{s['rule_id']} · affects {len(s['_giatas'])} hotels · {s['gen_text'][:120]}")
                if lev["system"]:
                    st.markdown(f"ℹ️ {len(lev['system'])} system-cost rule(s) shown as context only "
                                f"(never change these to chase win rate).")
                st.caption("Suggestions are starting points for a human to approve — the new value "
                           "is an approximate steer due to system→consumer markup layering.")

# ── 3. OVERVIEW ───────────────────────────────────────────────────────────────────
with tabs[2]:
    v = tab_filters(df, "ovw")
    vc = v[v["result"].isin(["Win", "Lose"])]
    wn = (v["result"] == "Win").sum(); ln = (v["result"] == "Lose").sum()
    ncn = (v["result"] == "Win - No Comp").sum(); nrn = (v["result"] == "No D2 Result").sum()
    total = wn + ln
    wr = wn / total * 100 if total else 0
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Hotels", v["hotel_name"].nunique())
    c2.metric("Win Rate", f"{wr:.0f}%", f"{wn}W/{ln}L")
    c3.metric("No Comp", int(ncn))
    hv_ov = aggregate_hotels(v)
    tot_bk = int(hv_ov["bk_bookings"].sum()) if not hv_ov.empty else 0
    c4.metric("Bookings", f"{tot_bk:,}")
    c5.metric("Total Clicks", f"{v.groupby('hotel_name')['clicks'].max().sum():.0f}")
    c6.metric("Avg Margin", f"{v[v['current_margin']>0]['current_margin'].mean():.1f}%")
    col1, col2 = st.columns(2)
    with col1:
        wl = v.groupby(["dep_window", "result"]).size().reset_index(name="n")
        fig = px.bar(wl, x="dep_window", y="n", color="result",
                     color_discrete_map={"Win": "#0A7C4E", "Lose": "#C0392B",
                                         "Win - No Comp": "#D4AF37", "No D2 Result": "#888"},
                     title="Results by Departure Window",
                     category_orders={"dep_window": ["0-60", "61-120", "121-240", "241+"]})
        fig.update_layout(height=320, margin=dict(t=40, b=10))
        st.plotly_chart(fig, use_container_width=True, key="ov_results_window")
    with col2:
        if not vc.empty:
            ds = vc.groupby("destination").apply(
                lambda g: (g["result"] == "Win").sum() / len(g) * 100).reset_index()
            ds.columns = ["Dest", "WinRate"]
            fig2 = px.bar(ds, x="Dest", y="WinRate", title="Win Rate by Destination (%)",
                          color="WinRate", color_continuous_scale=["#C0392B", "#D4AF37", "#0A7C4E"],
                          range_color=[30, 80])
            fig2.update_layout(height=320, margin=dict(t=40, b=10))
            st.plotly_chart(fig2, use_container_width=True, key="ov_winrate_dest")

    # Booking volume by destination (with win-rate overlay) — only when bookings loaded
    if not dest_bk.empty:
        st.markdown("#### Booking Volume by Destination")
        db = dest_bk.copy().sort_values("bookings", ascending=False)
        wr_by_dest = (vc.groupby("destination")
                      .apply(lambda g: (g["result"] == "Win").sum() / len(g) * 100)
                      if not vc.empty else pd.Series(dtype=float))
        db["win_rate"] = db["destination"].map(wr_by_dest)
        figb = go.Figure()
        figb.add_trace(go.Bar(x=db["destination"], y=db["bookings"], name="Bookings",
                              marker_color="#1B6FD4",
                              text=db["bookings"].map("{:,.0f}".format), textposition="outside"))
        if db["win_rate"].notna().any():
            figb.add_trace(go.Scatter(x=db["destination"], y=db["win_rate"], name="Win rate %",
                                      yaxis="y2", mode="lines+markers",
                                      line=dict(color="#0A7C4E", width=3)))
            figb.update_layout(yaxis2=dict(title="Win rate %", overlaying="y", side="right",
                                           range=[0, 100], showgrid=False))
        figb.update_layout(title="Bookings vs win rate by destination", height=360,
                           yaxis_title="Bookings", margin=dict(t=40, b=10),
                           legend=dict(orientation="h", y=1.12))
        st.plotly_chart(figb, use_container_width=True, key="ov_bookings_dest")

    st.markdown("#### Lead-In Competitors on Trivago")
    lc = v[v["lead_competitor"] != ""].groupby("lead_competitor").agg(
        Appearances=("hotel_name", "count"),
        Beats_Us=("result", lambda x: (x == "Lose").sum()),
        We_Beat=("result", lambda x: (x == "Win").sum())).reset_index()
    lc = lc.sort_values("Appearances", ascending=False).head(12)
    lc.columns = ["Lead-In Competitor", "Appearances", "Beats D2", "D2 Beats Them"]
    _stylc = apply_colors(lc.style, {"Beats D2": _const(CLR_RED),
                                     "D2 Beats Them": _const(CLR_GREEN)}, lc.columns)
    st.dataframe(_stylc, use_container_width=True)

# ── 4. HOTEL ACTIONS ──────────────────────────────────────────────────────────────
with tabs[3]:
    st.subheader("Individual Hotel Actions (search-level)")
    v = tab_filters(df, "ha", show_hotel=True)
    srch = st.text_input("Search hotel", placeholder="e.g. Atlantis…", key="s_ha")
    if srch:
        v = v[v["hotel_name"].str.contains(srch, case=False, na=False)]
    v = v.sort_values(["bk_bookings", "clicks", "diff_pct"], ascending=[False, False, True])
    v["Margin"] = v.apply(lambda r: margin_cell(r["current_margin"], r["margin_after"]), axis=1)
    show = v[["giata", "hotel_name", "destination", "board", "dep_window", "dep_month",
              "lead_competitor", "d2_price", "comp_price", "diff_pct", "result",
              "Margin", "clicks", "bk_bookings", "supplier"]].copy()
    show.columns = ["Giata", "Hotel", "Dest", "Board", "Window", "Month", "Lead-In Comp",
                    "D2 £", "Comp £", "Gap %", "Result", "Margin", "Clicks", "Bookings", "Supplier"]
    show = annotate(show)
    _sty = show.style.format({"D2 £": "{:.0f}", "Comp £": "{:.0f}",
                              "Gap %": "{:.1f}%", "Clicks": "{:.0f}", "Bookings": "{:.0f}"})
    _sty = apply_colors(_sty, {"Result": c_result, "Gap %": c_gap, "Margin": c_move,
                               "Offer": c_offer, "✅ Done": c_done}, show.columns)
    st.dataframe(_sty, use_container_width=True, height=600)
    inline_progress(v, "ha_prog")

# ── 5. RAISE MARGIN (Q2) ────────────────────────────────────────────────────────────
with tabs[4]:
    st.subheader("↑ Raise Margin — Winning + High Clicks + Headroom (Q2)")
    st.success("Strong demand and we're cheap → capture margin. Suggested move respects the 15% "
               "ceiling and keeps the win rate from dropping.")
    if hotels.empty:
        st.info("No data.")
    else:
        r = hotels[hotels["quadrant"].isin(["Q2 · Raise Margin", "Q2 · Hold (capped)"])].copy()
        r = r.sort_values(["bk_bookings", "clicks"], ascending=False)
        r["Margin Move"] = r.apply(fmt_move, axis=1)
        show = r[["giata", "hotel_name", "destination", "clicks", "bk_bookings", "sug_cur_wr",
                  "sug_pred_wr", "avg_diff_pct", "Margin Move", "sug_note"]].copy()
        show.columns = ["Giata", "Hotel", "Dest", "Clicks", "Bookings", "WR Now", "WR Est",
                        "Avg Gap %", "Margin (cur→suggested)", "Action"]
        show = annotate(show)
        _sty = show.style.format({"Clicks": "{:.0f}", "Bookings": "{:.0f}",
                                  "WR Now": "{:.0%}", "WR Est": "{:.0%}",
                                  "Avg Gap %": "{:.1f}%"})
        _sty = apply_colors(_sty, {"WR Now": c_winrate, "WR Est": c_winrate,
                                   "Avg Gap %": c_gap, "Margin (cur→suggested)": c_move,
                                   "Action": c_action, "Offer": c_offer, "✅ Done": c_done}, show.columns)
        st.dataframe(_sty, use_container_width=True, height=520)
        inline_progress(r, "rai_prog")

# ── 6. REDUCE PRICE (Q3) ────────────────────────────────────────────────────────────
with tabs[5]:
    st.subheader("↓ Reduce Price — Losing + Low Clicks + Margin Room (Q3)")
    if hotels.empty:
        st.info("No data.")
    else:
        r = hotels[hotels["quadrant"].isin(["Q3 · Reduce Price", "Reduce to Convert"])].copy()
        st.warning(f"{len(r)} reduce candidates. Suggested cut is capped at the {MARGIN_FLOOR:.0f}% "
                   f"floor and ±5pp; hotels needing a deeper cut appear under Product/Cost Fix.")
        r = r.sort_values("clicks", ascending=True)
        r["Margin Move"] = r.apply(fmt_move, axis=1)
        show = r[["giata", "hotel_name", "destination", "clicks", "bk_bookings", "sug_cur_wr",
                  "sug_pred_wr", "avg_diff_pct", "Margin Move", "sug_note"]].copy()
        show.columns = ["Giata", "Hotel", "Dest", "Clicks", "Bookings", "WR Now", "WR Est",
                        "Avg Gap %", "Margin (cur→suggested)", "Action"]
        show = annotate(show)
        _sty = show.style.format({"Clicks": "{:.0f}", "Bookings": "{:.0f}",
                                  "WR Now": "{:.0%}", "WR Est": "{:.0%}",
                                  "Avg Gap %": "{:.1f}%"})
        _sty = apply_colors(_sty, {"WR Now": c_winrate, "WR Est": c_winrate,
                                   "Avg Gap %": c_gap, "Margin (cur→suggested)": c_move,
                                   "Action": c_action, "Offer": c_offer, "✅ Done": c_done}, show.columns)
        st.dataframe(_sty, use_container_width=True, height=520)
        inline_progress(r, "red_prog")

# ── 7. VISIBILITY GAP (Q1) ──────────────────────────────────────────────────────────
with tabs[6]:
    st.subheader("👁 Visibility Gap — Winning on Price but NOT Getting Clicks (Q1)")
    st.info("We're competitive but demand isn't landing. This is a content / position / "
            "exposure issue — NOT a price problem. Do not cut price here.")
    if hotels.empty:
        st.info("No data.")
    else:
        r = hotels[hotels["quadrant"] == "Q1 · Visibility Gap"].copy()
        r = r.sort_values(["win_rate", "clicks"], ascending=[False, True])
        r["Margin %"] = r["current_margin"].map("{:.1f}%".format)
        show = r[["giata", "hotel_name", "destination", "clicks", "bk_bookings", "win_rate",
                  "avg_diff_pct", "Margin %", "searches"]].copy()
        show.columns = ["Giata", "Hotel", "Dest", "Clicks", "Bookings", "Win Rate", "Avg Gap %",
                        "Margin", "Searches"]
        show = annotate(show)
        _sty = show.style.format({"Clicks": "{:.0f}", "Bookings": "{:.0f}",
                                  "Win Rate": "{:.0%}", "Avg Gap %": "{:.1f}%"})
        _sty = apply_colors(_sty, {"Win Rate": c_winrate, "Avg Gap %": c_gap,
                                   "Offer": c_offer, "✅ Done": c_done}, show.columns)
        st.dataframe(_sty, use_container_width=True, height=520)
        inline_progress(r, "vis_prog")

# ── 8. PRODUCT / COST FIX (Q4) ──────────────────────────────────────────────────────
with tabs[7]:
    st.subheader("🔧 Product / Cost Fix — Unreachable on Price (Q4)")
    st.error("Where matching the competitor would need a markup **below the 7% floor**, the hotel "
             "is unreachable on price — a product/contracting problem, not a markup one. **Never "
             "price-cut these.** A hotel is flagged 🚫 Product Gap when ≥50% of its losses are "
             "below floor and there are at least 2 such losses.")
    if hotels.empty:
        st.info("No data.")
    else:
        pg = hotels[hotels["product_gap"]].copy()
        other = hotels[(~hotels["product_gap"]) &
                       (hotels["quadrant"].isin(["Q4 · Product/Cost Fix", "Cost Fix (high demand)"]))].copy()
        c1, c2 = st.columns(2)
        c1.metric("🚫 Product-gap hotels", len(pg))
        c2.metric("Bookings at risk", f"{int(pg['bk_bookings'].sum()):,}")

        st.markdown("#### 🚫 Product Gap — unreachable on price")
        if pg.empty:
            st.caption("No hotels meet the product-gap test in the current selection.")
        else:
            pg["Verdict"] = "🚫 Product Gap — unreachable on price"
            pg = pg.sort_values(["bk_bookings", "loses"], ascending=False)
            show = pg[["giata", "hotel_name", "destination", "loses", "n_below_floor",
                       "pct_below_floor", "avg_loss_gap", "avg_mtm", "bk_bookings", "Verdict"]].copy()
            show.columns = ["Giata", "Hotel", "Dest", "Loses", "Below Floor", "% Below Floor",
                            "Avg Gap %", "Avg markup-to-match", "Bookings", "Verdict"]
            show = annotate(show)
            _sty = show.style.format({"% Below Floor": "{:.0f}%", "Avg Gap %": "{:.1f}%",
                                      "Avg markup-to-match": "{:.1f}%", "Bookings": "{:.0f}"})
            _sty = apply_colors(_sty, {"Avg Gap %": c_gap, "% Below Floor": _const(CLR_RED),
                                       "Verdict": _const(CLR_RED), "Offer": c_offer, "✅ Done": c_done}, show.columns)
            st.dataframe(_sty, use_container_width=True, height=420)
            inline_progress(pd.concat([pg, other]).drop_duplicates("giata"), "pc_prog")

        st.markdown("#### Other losing hotels with no clicks / no margin room")
        if other.empty:
            st.caption("None in the current selection.")
        else:
            other = other.sort_values("clicks", ascending=False)
            other["Margin %"] = other["current_margin"].map("{:.1f}%".format)
            show2 = other[["giata", "hotel_name", "destination", "clicks", "bk_bookings",
                           "win_rate", "avg_diff_pct", "Margin %", "decision"]].copy()
            show2.columns = ["Giata", "Hotel", "Dest", "Clicks", "Bookings", "Win Rate",
                             "Avg Gap %", "Margin", "Action"]
            _sty2 = show2.style.format({"Clicks": "{:.0f}", "Bookings": "{:.0f}",
                                        "Win Rate": "{:.0%}", "Avg Gap %": "{:.1f}%"})
            _sty2 = apply_colors(_sty2, {"Win Rate": c_winrate, "Avg Gap %": c_gap,
                                         "Action": c_action}, show2.columns)
            st.dataframe(_sty2, use_container_width=True, height=320)

# ── 9. NO COMP ──────────────────────────────────────────────────────────────────────
with tabs[8]:
    st.subheader("No Direct Comparison on Trivago")
    st.info("D2 has a price but no competitor was returned. Price to destination norms; protect margin.")
    v = tab_filters(df, "nc")
    nc = v[v["result"] == "Win - No Comp"].copy()
    nc["Margin %"] = nc["current_margin"].map("{:.1f}%".format)
    show = nc[["giata", "hotel_name", "destination", "board", "dep_window", "dep_month",
               "d2_price", "Margin %", "clicks", "bk_bookings", "supplier"]].copy()
    show.columns = ["Giata", "Hotel", "Dest", "Board", "Window", "Month", "D2 £", "Margin",
                    "Clicks", "Bookings", "Supplier"]
    show = annotate(show)
    _sty = show.style.format({"D2 £": "{:.0f}", "Clicks": "{:.0f}", "Bookings": "{:.0f}"})
    _sty = apply_colors(_sty, {"Margin": _const(CLR_ORANGE), "Offer": c_offer, "✅ Done": c_done}, show.columns)
    st.dataframe(_sty, use_container_width=True, height=480)
    inline_progress(nc, "nc_prog")
    st.markdown("#### No D2 Result — Availability / Product Gaps")
    nores = v[v["result"] == "No D2 Result"].groupby(["giata", "hotel_name", "destination"]).agg(
        Searches=("hotel_name", "count"),
        Clicks=("clicks", "max"),
        Bookings=("bk_bookings", "max")).reset_index().sort_values(
        ["Bookings", "Clicks"], ascending=False)
    nores.columns = ["Giata", "Hotel", "Dest", "No-Result Searches", "Clicks", "Bookings"]
    _styn = apply_colors(nores.style.format({"Clicks": "{:.0f}", "Bookings": "{:.0f}"}),
                         {"No-Result Searches": _const(CLR_ORANGE)}, nores.columns)
    st.dataframe(_styn, use_container_width=True, height=300)

# ── 10. SUPPLIERS ────────────────────────────────────────────────────────────────────
with tabs[9]:
    st.subheader("Supplier Performance")
    v = tab_filters(df, "sup")
    sup = v[v["supplier"].astype(str).str.strip().ne("") & v["supplier"].ne("-")].copy()
    if sup.empty:
        st.info("No supplier data in current selection.")
    else:
        sup["supplier_brand"] = sup["supplier"].apply(merge_supplier_name)
        agg = sup.groupby("supplier_brand").agg(
            Rows=("hotel_name", "count"),
            Hotels=("hotel_name", "nunique"),
            Wins=("result", lambda x: (x == "Win").sum()),
            Loses=("result", lambda x: (x == "Lose").sum()),
            AvgMargin=("current_margin", lambda x: x[x > 0].mean())).reset_index()
        # bookings per supplier = sum over distinct hotels (giata) to avoid row double-count
        bk_by_sup = (sup.drop_duplicates(["supplier_brand", "giata"])
                     .groupby("supplier_brand")["bk_bookings"].sum())
        agg["Bookings"] = agg["supplier_brand"].map(bk_by_sup).fillna(0)
        agg["Win Rate"] = (agg["Wins"] / (agg["Wins"] + agg["Loses"]).replace(0, np.nan) * 100)
        agg = agg.sort_values("Rows", ascending=False)
        agg["Win Rate"] = agg["Win Rate"].map(lambda x: f"{x:.0f}%" if pd.notna(x) else "—")
        agg["AvgMargin"] = agg["AvgMargin"].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "—")
        agg = agg[["supplier_brand", "Rows", "Hotels", "Bookings", "Wins", "Loses", "AvgMargin", "Win Rate"]]
        agg.columns = ["Supplier", "Rows", "Hotels", "Bookings", "Wins", "Loses",
                       "Avg Margin", "Win Rate"]
        st.caption("Supplier codes after the dash are merged to the parent supplier "
                   "(e.g. OTS - AMXCUN75FI → OTS).")
        _sty = apply_colors(agg.style.format({"Bookings": "{:.0f}"}),
                            {"Wins": _const(CLR_GREEN), "Loses": _const(CLR_RED),
                             "Win Rate": c_winrate}, agg.columns)
        st.dataframe(_sty, use_container_width=True, height=520)

# ── 11. PRICE & CLICKS TRENDS ─────────────────────────────────────────────────────────
with tabs[10]:
    st.subheader("↗ Price & Clicks Trends")
    v = tab_filters(df, "trd", show_hotel=True)
    st.markdown("---")

    # 2a — headline "By Travel Month & Destination"
    st.markdown("#### By Travel Month & Destination")
    st.caption("Grouped columns per destination by departure/travel month, with up to two "
               "per-destination overlay lines (each on its own axis). Win rate uses real "
               "comparisons only; values rounded to 2 dp.")
    mc1, mc2, mc3 = st.columns(3)
    cols_m = mc1.selectbox("Columns show", _METRIC_OPTS, key="tmd_cols")
    line1 = mc2.selectbox("Overlay line 1 (dashed)", ["None"] + _METRIC_OPTS, key="tmd_l1")
    line2 = mc3.selectbox("Overlay line 2 (dotted)", ["None"] + _METRIC_OPTS, key="tmd_l2")
    _bm_view = (bmonths_df[bmonths_df["giata"].astype(str).isin(set(v["giata"].astype(str)))]
                if (bmonths_df is not None and not bmonths_df.empty and not v.empty) else None)
    _fig_tmd, _note = build_tmd_fig(tmd_aggregate(v, _bm_view), cols_m, line1, line2)
    if _fig_tmd is not None:
        st.plotly_chart(_fig_tmd, use_container_width=True, key="tmd_main")
        if _note:
            st.caption("ℹ️ " + _note)
    else:
        st.info(_note or "No travel-month data for this selection.")

    # 2b — focused view (filter instead of adding axes), with YoY toggle
    with st.expander("🔍 Focused view — filter scope (run dates, windows, month ranges, YoY)"):
        all_dests = sorted(v["destination"].unique().tolist())
        all_files = sorted(v["file_name"].unique().tolist())
        fc1, fc2, fc3 = st.columns(3)
        f_dests = fc1.multiselect("Destination(s)", all_dests, default=all_dests, key="fv_dest")
        f_wins = fc2.multiselect("Departure window(s)", ["0-60", "61-120", "121-240", "241+"],
                                 default=["0-60", "61-120", "121-240", "241+"], key="fv_win")
        f_files = fc3.multiselect("Comparison run date(s)", all_files, default=all_files, key="fv_files")
        fv = v[v["destination"].isin(f_dests) & v["dep_window"].isin(f_wins)
               & v["file_name"].isin(f_files)].copy()
        bm_fv = (bmonths_df[bmonths_df["giata"].astype(str).isin(set(fv["giata"].astype(str)))].copy()
                 if (bmonths_df is not None and not bmonths_df.empty and not fv.empty) else None)
        # booked-month & departure-month range sliders
        if bm_fv is not None and not bm_fv.empty:
            bk_months = sorted([m for m in bm_fv["booked_month"].unique() if m])
            dp_months = sorted([m for m in bm_fv["dep_month"].unique() if m])
            if len(bk_months) >= 2:
                bk_lo, bk_hi = st.select_slider("Booked-month range", options=bk_months,
                                                value=(bk_months[0], bk_months[-1]), key="fv_bk")
                bm_fv = bm_fv[(bm_fv["booked_month"] >= bk_lo) & (bm_fv["booked_month"] <= bk_hi)]
            if len(dp_months) >= 2:
                dp_lo, dp_hi = st.select_slider("Departure-month range", options=dp_months,
                                                value=(dp_months[0], dp_months[-1]), key="fv_dp")
                bm_fv = bm_fv[(bm_fv["dep_month"] >= dp_lo) & (bm_fv["dep_month"] <= dp_hi)]
        sc1, sc2, sc3, sc4 = st.columns(4)
        f_cols = sc1.selectbox("Columns show", _METRIC_OPTS, key="fv_cols")
        f_l1 = sc2.selectbox("Overlay line 1 (dashed)", ["None"] + _METRIC_OPTS, key="fv_l1")
        f_l2 = sc3.selectbox("Overlay line 2 (dotted)", ["None"] + _METRIC_OPTS, key="fv_l2")
        f_yoy = sc4.checkbox("Year-on-Year", key="fv_yoy",
                             help="X-axis becomes month-of-year; each series is a departure year.")
        long_fv = tmd_aggregate(fv, bm_fv)
        if len(dp_months if (bm_fv is not None and not bm_fv.empty) else []) and not long_fv.empty:
            pass
        _fig_fv, _note_fv = build_tmd_fig(long_fv, f_cols, f_l1, f_l2, yoy=f_yoy)
        if _fig_fv is not None:
            st.plotly_chart(_fig_fv, use_container_width=True, key="tmd_focused")
            if _note_fv:
                st.caption("ℹ️ " + _note_fv)
        else:
            st.info(_note_fv or "No data for this focused selection.")

    st.markdown("---")
    if not v.empty:
        sc = v.copy()
        sc["d2_price"] = pd.to_numeric(sc["d2_price"], errors="coerce")
        sc["comp_price"] = pd.to_numeric(sc["comp_price"], errors="coerce")
        sc = sc[sc["d2_price"] > 0].dropna(subset=["check_in_dt"]).sort_values("check_in_dt")
        if sc.empty:
            st.warning("No valid price/date data for this selection.")
        else:
            grp = sc.groupby("check_in_dt", as_index=False).agg(
                D2=("d2_price", "mean"),
                Comp=("comp_price", lambda x: x[x > 0].mean() if (x > 0).any() else np.nan))
            dest_lbl = ", ".join(v["destination"].unique().tolist())
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=grp["check_in_dt"], y=grp["D2"], name="D2 price £",
                                     mode="lines+markers", line=dict(color="#1B6FD4", width=3)))
            cv = grp[grp["Comp"].notna()]
            if not cv.empty:
                fig.add_trace(go.Scatter(x=cv["check_in_dt"], y=cv["Comp"], name="Lead-in comp £",
                                         mode="lines+markers",
                                         line=dict(color="#E04A3F", width=2, dash="dot")))
            fig.update_layout(title=f"Avg D2 vs Lead-In Competitor Price — {dest_lbl}",
                              xaxis=dict(title="Departure Date", type="date"),
                              yaxis_title="Price £", height=380, hovermode="x unified",
                              legend=dict(orientation="h", y=1.02))
            st.plotly_chart(fig, use_container_width=True, key="trd_price")

    # Bookings by month — booked vs departure/travel (giata-scoped to hotels in view)
    if bmonths_df is not None and not bmonths_df.empty and not v.empty:
        giatas_in_view = set(v["giata"].astype(str))
        bm = bmonths_df[bmonths_df["giata"].astype(str).isin(giatas_in_view)].copy()
        if not bm.empty:
            dep = bm[bm["dep_month"] != ""].groupby("dep_month")["bookings"].sum()
            bkd = bm[bm["booked_month"] != ""].groupby("booked_month")["bookings"].sum()
            months = sorted(set(dep.index) | set(bkd.index))
            if months:
                def _ml(m):
                    return pd.to_datetime(m + "-01").strftime("%b %Y") if len(m) == 7 else m
                labels = [_ml(m) for m in months]
                figbm = go.Figure()
                figbm.add_trace(go.Bar(x=labels, y=[float(dep.get(m, 0)) for m in months],
                                       name="By departure / travel month", marker_color="#0A7C4E"))
                figbm.add_trace(go.Bar(x=labels, y=[float(bkd.get(m, 0)) for m in months],
                                       name="By booked month", marker_color="#1B6FD4"))
                figbm.update_layout(title="Bookings by Month — booked vs departure/travel",
                                    barmode="group", height=340, yaxis_title="Bookings",
                                    legend=dict(orientation="h", y=1.14), margin=dict(t=50, b=10))
                st.plotly_chart(figbm, use_container_width=True, key="trd_bookings_month")
                st.caption("🟩 Green = when guests travel (departure/stay month) · "
                           "🟦 Blue = when the booking was made (booked month).")

    st.markdown("---")
    col3, col4 = st.columns(2)
    with col3:
        wr_src = v[v["result"].isin(["Win", "Lose"])].copy()
        if not wr_src.empty and wr_src["dep_month"].ne("").any():
            wr_m = wr_src.groupby("dep_month").apply(
                lambda g: round((g["result"] == "Win").sum() / len(g) * 100, 2)).reset_index()
            wr_m.columns = ["Month", "WinRate"]
            wr_m = wr_m[wr_m["Month"] != ""].sort_values("Month")
            wr_m["Label"] = wr_m["Month"].apply(
                lambda m: pd.to_datetime(m + "-01").strftime("%b %Y") if len(m) == 7 else m)
            wr_m["Colour"] = wr_m["WinRate"].apply(lambda v2: "#C0392B" if v2 < 50 else "#27AE60")
            # avg markup by departure month (the lever) — logged against the same months
            mk_src = v[v["current_margin"] > 0]
            mk_m = (mk_src.groupby("dep_month")["current_margin"].mean().round(2)
                    if not mk_src.empty else pd.Series(dtype=float))
            wr_m["Markup"] = wr_m["Month"].map(mk_m)
            # bookings by departure month (overlay)
            bk_m = (_bm_view[_bm_view["dep_month"] != ""].groupby("dep_month")["bookings"].sum()
                    if (_bm_view is not None and not _bm_view.empty) else pd.Series(dtype=float))
            wr_m["Bookings"] = wr_m["Month"].map(bk_m).fillna(0)
            fig_wr = go.Figure()
            fig_wr.add_trace(go.Bar(x=wr_m["Label"], y=wr_m["WinRate"], marker_color=wr_m["Colour"],
                                    name="Win rate %"))
            if wr_m["Markup"].notna().any():
                fig_wr.add_trace(go.Scatter(x=wr_m["Label"], y=wr_m["Markup"], name="Avg markup %",
                                            yaxis="y2", mode="lines+markers",
                                            line=dict(color="#D4AF37", width=3)))
                fig_wr.update_layout(yaxis2=dict(title="Avg markup %", overlaying="y", side="right",
                                                 showgrid=False, range=[0, max(20, wr_m["Markup"].max() + 3)]))
            if wr_m["Bookings"].sum() > 0:
                fig_wr.add_trace(go.Scatter(x=wr_m["Label"], y=wr_m["Bookings"], name="Bookings",
                                            yaxis="y3", mode="lines+markers",
                                            line=dict(color="#0A7C4E", width=2, dash="dot")))
                fig_wr.update_layout(xaxis=dict(domain=[0.0, 0.90]),
                                     yaxis3=dict(title="Bookings", overlaying="y", side="right",
                                                 anchor="free", position=1.0, showgrid=False))
            fig_wr.add_hline(y=50, line_dash="dot", line_color="#E04A3F")
            fig_wr.update_layout(title="Win Rate · Avg Markup · Bookings by Departure Month",
                                 yaxis=dict(range=[0, 100], title="Win Rate %"),
                                 xaxis_title="Departure Month", height=360,
                                 legend=dict(orientation="h", y=1.16), margin=dict(t=55, b=10))
            st.plotly_chart(fig_wr, use_container_width=True, key="trd_winrate_markup")
    with col4:
        hv = aggregate_hotels(v)
        if not hv.empty:
            top = hv[(hv["clicks"] > 0) | (hv["bk_bookings"] > 0)].copy()
            top = top.sort_values(["bk_bookings", "clicks"], ascending=False).head(15)
            if not top.empty:
                has_bk = bool(top["bk_bookings"].sum() > 0)
                fig_c = go.Figure()
                fig_c.add_trace(go.Bar(x=top["hotel_name"], y=top["clicks"],
                                       marker_color="#1B6FD4", name="Clicks"))
                if has_bk:
                    fig_c.add_trace(go.Bar(x=top["hotel_name"], y=top["bk_bookings"],
                                           marker_color="#0A7C4E", name="Bookings", yaxis="y2"))
                    fig_c.update_layout(yaxis2=dict(title="Bookings", overlaying="y",
                                                    side="right", showgrid=False))
                fig_c.update_layout(title="Top Hotels — Clicks & Bookings",
                                    height=360, xaxis_tickangle=-40, yaxis_title="Clicks",
                                    barmode="group", legend=dict(orientation="h", y=1.12),
                                    showlegend=has_bk)
                st.plotly_chart(fig_c, use_container_width=True, key="trd_top_hotels")

    # 2c — Booking Pace by Booked Month (bookings bars + revenue line) — when bookings were made
    if _bm_view is not None and not _bm_view.empty and (_bm_view["booked_month"] != "").any():
        pace = _bm_view[_bm_view["booked_month"] != ""].groupby("booked_month", as_index=False).agg(
            Bookings=("bookings", "sum"), Revenue=("revenue", "sum")).sort_values("booked_month")
        if not pace.empty:
            pace["Label"] = pace["booked_month"].apply(_month_label)
            fig_pace = go.Figure()
            fig_pace.add_trace(go.Bar(x=pace["Label"], y=pace["Bookings"], name="Bookings",
                                      marker_color="#1B6FD4"))
            if pace["Revenue"].sum() > 0:
                fig_pace.add_trace(go.Scatter(x=pace["Label"], y=pace["Revenue"], name="Revenue £",
                                              yaxis="y2", mode="lines+markers",
                                              line=dict(color="#0A7C4E", width=3)))
                fig_pace.update_layout(yaxis2=dict(title="Revenue £", overlaying="y",
                                                   side="right", showgrid=False))
            fig_pace.update_layout(title="Booking Pace by Booked Month",
                                   yaxis_title="Bookings", xaxis_title="Booked Month",
                                   height=340, legend=dict(orientation="h", y=1.14),
                                   margin=dict(t=50, b=10))
            st.plotly_chart(fig_pace, use_container_width=True, key="trd_booking_pace")
            st.caption("When bookings were made (booked month) and the revenue they brought in.")

# ── 12. FILES ─────────────────────────────────────────────────────────────────────────
with tabs[11]:
    st.subheader("📂 Uploaded Files")
    fl = get_file_list()
    if fl:
        fdf = pd.DataFrame(fl, columns=["File", "Platform", "Run Date", "Destination", "Rows"])
        st.dataframe(fdf, use_container_width=True)
        st.caption(f"{len(fdf)} files · {fdf['Rows'].sum():,} comparison rows")
        st.download_button("⬇ Download enriched data (CSV)",
                           data=df.to_csv(index=False),
                           file_name="trivago_pricing_clicks_export.csv", mime="text/csv")
        if not hotels.empty:
            st.download_button("⬇ Download decision matrix + margin suggestions (CSV)",
                               data=hotels.to_csv(index=False),
                               file_name="trivago_decision_matrix.csv", mime="text/csv")
    else:
        st.info("No files uploaded yet.")

# ── 13. PROGRESS (shared completion tracking) ─────────────────────────────────────────
with tabs[12]:
    st.subheader("✅ Progress — Shared Completion Tracking")
    st.caption("Pick the pricing-data date you're actioning against, tick off the hotels you've "
               "handled, add your initials and a note, and save. The log is shared in the app "
               "database so everyone viewing sees the same state.")
    if hotels.empty or df.empty:
        st.info("Upload comparison data to start tracking progress.")
    else:
        dates = sorted([d for d in df["file_date"].dropna().astype(str).unique() if d], reverse=True)
        if not dates:
            st.info("No pricing-data dates available.")
        else:
            top_c = st.columns([1.2, 2, 1])
            data_date = top_c[0].selectbox("Pricing data date", dates, index=0, key="prog_date")
            scope_dests = sorted(hotels["destination"].dropna().unique().tolist())
            sel_pd = top_c[1].multiselect("Destination(s) in scope", scope_dests,
                                          default=scope_dests, key="prog_dest")
            movers_only = top_c[2].checkbox("Only suggested changes", value=True, key="prog_movers")
            st.caption("Departure-window scope follows the sidebar's global Dep Window filter.")

            scope = hotels[hotels["destination"].isin(sel_pd)].copy()
            if movers_only:
                scope = scope[scope["sug_delta"].abs() >= 0.5]
            scope = scope.sort_values(["demand_score", "bk_bookings", "clicks"], ascending=False)

            if scope.empty:
                st.info("No in-scope hotels for this selection.")
            else:
                scope["Suggested Δ"] = scope.apply(fmt_move, axis=1)
                # pre-fill from existing completions for this data date
                existing = comp_log[comp_log["data_date"].astype(str) == str(data_date)] \
                    if (comp_log is not None and not comp_log.empty) else pd.DataFrame()
                ex_map = {str(r["giata"]): r for _, r in existing.iterrows()} if not existing.empty else {}
                grid = pd.DataFrame({
                    "Giata": scope["giata"].astype(str).values,
                    "Hotel": scope["hotel_name"].values,
                    "Dest": scope["destination"].values,
                    "Suggested Δ": scope["Suggested Δ"].values,
                    "✅ Done": [bool(ex_map.get(g, {}).get("done", 0)) for g in scope["giata"].astype(str)],
                    "Initials": [str(ex_map.get(g, {}).get("initials", "") or "") for g in scope["giata"].astype(str)],
                    "Note": [str(ex_map.get(g, {}).get("note", "") or "") for g in scope["giata"].astype(str)],
                })
                edited = st.data_editor(
                    grid, hide_index=True, use_container_width=True, height=460, key="prog_editor",
                    column_config={
                        "Giata": st.column_config.TextColumn(disabled=True),
                        "Hotel": st.column_config.TextColumn(disabled=True),
                        "Dest": st.column_config.TextColumn(disabled=True),
                        "Suggested Δ": st.column_config.TextColumn("Suggested Δ", disabled=True),
                        "✅ Done": st.column_config.CheckboxColumn("✅ Done"),
                        "Initials": st.column_config.TextColumn("Initials", max_chars=8),
                        "Note": st.column_config.TextColumn("Note — what you changed", max_chars=300),
                    })
                csave, cinfo = st.columns([1, 3])
                if csave.button("💾 Save progress", key="prog_save"):
                    smap = dict(zip(scope["giata"].astype(str), scope["Suggested Δ"]))
                    rows = []
                    for _, er in edited.iterrows():
                        g = str(er["Giata"])
                        done = bool(er["✅ Done"])
                        note = str(er.get("Note", "") or "").strip()
                        initials = str(er.get("Initials", "") or "").strip()
                        if done or note or initials:   # only write rows that carry information
                            rows.append({"giata": g, "hotel_name": er["Hotel"],
                                         "destination": er["Dest"], "data_date": str(data_date),
                                         "done": int(done), "suggested_action": smap.get(g, ""),
                                         "note": note, "initials": initials})
                    saved = save_completions(rows)
                    cinfo.success(f"Saved {saved} update(s) for data date {data_date}. Refresh to see ✅ flags update.")

            # ── Action log (full history, foundation for impact tracking) ──────────
            st.markdown("#### Action Log")
            full_log = load_completions()
            if full_log is None or full_log.empty:
                st.caption("No actions logged yet.")
            else:
                log = full_log.copy().sort_values("updated_at", ascending=False)
                log["Done"] = log["done"].map(lambda x: "✅" if int(x) == 1 else "—")
                disp = log[["data_date", "hotel_name", "destination", "Done",
                            "suggested_action", "note", "initials", "updated_at"]].copy()
                disp.columns = ["Data Date", "Hotel", "Dest", "Done", "Action Taken",
                                "Note", "Who", "Updated"]
                st.dataframe(apply_colors(disp.style, {"Done": c_done}, disp.columns),
                             use_container_width=True, height=300)
                lc1, lc2 = st.columns([1, 3])
                lc1.download_button("⬇ Export action log (CSV)", data=disp.to_csv(index=False),
                                    file_name="trivago_action_log.csv", mime="text/csv",
                                    key="prog_export")
                with lc2.expander("⚠ Clear the action log (irreversible)"):
                    st.caption("This wipes ALL completion history. It is kept separate from "
                               "'Clear ALL data' so re-uploading pricing files never erases your log.")
                    if st.checkbox("Yes, I understand this deletes all history", key="prog_clear_ok"):
                        if st.button("🗑 Clear action log now", key="prog_clear"):
                            clear_completions()
                            st.success("Action log cleared."); st.rerun()

# ── 14. GUIDE ─────────────────────────────────────────────────────────────────────────
with tabs[13]:
    st.markdown(GUIDE_MD)
    st.download_button("⬇ Download this guide (Markdown)", data=GUIDE_MD,
                       file_name="D2_Trivago_Dashboard_Guide.md", mime="text/markdown")
