import streamlit as st
import pdfplumber
import re
import pandas as pd
import sqlite3
from datetime import datetime, date
import calendar

# ---------------------------------------------------------
# DATABASE SETUP
# ---------------------------------------------------------
def init_db():
    conn = sqlite3.connect("inventory.db")
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_id TEXT,
            drug_name TEXT,
            batch_no TEXT,
            mfg_date TEXT,
            expiry_date TEXT,
            quantity INTEGER,
            upload_date TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_items_to_db(voucher_id, items):
    conn = sqlite3.connect("inventory.db")
    c = conn.cursor()
    upload_date = date.today().isoformat()
    for item in items:
        c.execute('''
            INSERT INTO inventory (voucher_id, drug_name, batch_no, mfg_date, expiry_date, quantity, upload_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (voucher_id, item['drug_name'], item['batch_no'], item['mfg_date'], item['expiry_date'], item['quantity'], upload_date))
    conn.commit()
    conn.close()

def fetch_inventory():
    conn = sqlite3.connect("inventory.db")
    df = pd.read_sql_query("SELECT * FROM inventory", conn)
    conn.close()
    return df

# ---------------------------------------------------------
# PDF EXTRACTION ENGINE
# ---------------------------------------------------------
def parse_date(date_str):
    formats = ["%d/%m/%Y", "%d-%m-%Y", "%m/%Y", "%m-%Y", "%Y-%m-%d"]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if fmt in ["%m/%Y", "%m-%Y"]:
                _, last_day = calendar.monthrange(dt.year, dt.month)
                dt = dt.replace(day=last_day)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str

def extract_drug_data_from_pdf(pdf_file):
    extracted_items = []
    
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    clean_row = [str(cell).strip() for cell in row if cell is not None]
                    if len(clean_row) >= 4:
                        dates = [c for c in clean_row if re.search(r'\b\d{1,2}[/-]\d{2,4}\b', c)]
                        if len(dates) >= 1:
                            extracted_items.append({
                                "drug_name": clean_row[0] if len(clean_row) > 0 else "Unknown Drug",
                                "batch_no": clean_row[1] if len(clean_row) > 1 else "N/A",
                                "mfg_date": parse_date(dates[0]) if len(dates) > 1 else "N/A",
                                "expiry_date": parse_date(dates[-1]),
                                "quantity": int(clean_row[-1]) if clean_row[-1].isdigit() else 100
                            })
            
            if not extracted_items:
                text = page.extract_text()
                if text:
                    lines = text.split('\n')
                    for line in lines:
                        match = re.search(r'(?P<name>[A-Za-z0-9\s]+)\s+(?P<batch>[A-Z0-9\-\/]+)\s+(?P<exp>\d{1,2}[/-]\d{2,4})\s+(?P<qty>\d+)', line)
                        if match:
                            gd = match.groupdict()
                            extracted_items.append({
                                "drug_name": gd['name'].strip(),
                                "batch_no": gd['batch'].strip(),
                                "mfg_date": "N/A",
                                "expiry_date": parse_date(gd['exp']),
                                "quantity": int(gd['qty'])
                            })
                            
    return extracted_items

# ---------------------------------------------------------
# STREAMLIT UI
# ---------------------------------------------------------
st.set_page_config(page_title="CMS Drug Expiry Tracker", layout="wide", page_icon="💊")

init_db()

st.title("💊 Central Medical Store - Drug Expiry Tracker")
st.markdown("Upload voucher copy PDFs to track stock batches and monitor impending drug expiries.")

st.sidebar.header("⚙️ Settings & Alert Rules")
warning_days = st.sidebar.slider("Warning Threshold (Days)", min_value=30, max_value=180, value=90, step=15)
critical_days = st.sidebar.slider("Critical Threshold (Days)", min_value=7, max_value=60, value=30, step=7)

tab1, tab2, tab3 = st.tabs(["📤 Upload Voucher PDF", "⚠️ Expiry Alerts & Status", "📦 Full Inventory Records"])

with tab1:
    st.subheader("Upload Central Medical Store Voucher")
    uploaded_file = st.file_uploader("Select a PDF voucher file", type=["pdf"])
    
    if uploaded_file is not None:
        st.info("Parsing PDF content...")
        parsed_data = extract_drug_data_from_pdf(uploaded_file)
        
        if parsed_data:
            st.success(f"Successfully extracted {len(parsed_data)} items from voucher.")
            df_preview = pd.DataFrame(parsed_data)
            
            st.write("### Extracted Items Preview")
            edited_df = st.data_editor(df_preview, num_rows="dynamic")
            
            voucher_ref = st.text_input("Enter Voucher / Invoice Reference No.", value=f"VOUCHER-{datetime.now().strftime('%Y%m%d%H%M')}")
            
            if st.button("Save Voucher to Inventory Database"):
                save_items_to_db(voucher_ref, edited_df.to_dict('records'))
                st.success("Voucher items stored in database successfully!")
                st.rerun()
        else:
            st.warning("Could not automatically parse structured batch data. Check if the PDF is scannable or text-based.")

with tab2:
    st.subheader("Impending Expiry Dashboard")
    df_inv = fetch_inventory()
    
    if not df_inv.empty:
        today = date.today()
        df_inv['exp_date_dt'] = pd.to_datetime(df_inv['expiry_date'], errors='coerce').dt.date
        df_inv['days_until_expiry'] = (df_inv['exp_date_dt'] - today).apply(lambda x: x.days if pd.notnull(x) else 9999)
        
        def categorize(days):
            if days <= 0:
                return "Expired"
            elif days <= critical_days:
                return f"Critical (< {critical_days} days)"
            elif days <= warning_days:
                return f"Warning (< {warning_days} days)"
            else:
                return "Safe"
                
        df_inv['status'] = df_inv['days_until_expiry'].apply(categorize)
        
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Batches", len(df_inv))
        col2.metric("Expired", len(df_inv[df_inv['status'] == "Expired"]))
        col3.metric("Critical Warnings", len(df_inv[df_inv['status'].str.contains("Critical")]))
        col4.metric("Approaching Warnings", len(df_inv[df_inv['status'].str.contains("Warning")]))
        
        st.divider()
        alerts_df = df_inv[df_inv['days_until_expiry'] <= warning_days].sort_values("days_until_expiry")
        
        if not alerts_df.empty:
            st.warning(f"Found {len(alerts_df)} batch(es) requiring immediate attention.")
            
            def highlight_expiry(val):
                if val == "Expired":
                    return 'background-color: #ff4b4b; color: white; font-weight: bold;'
                elif "Critical" in str(val):
                    return 'background-color: #ffa726; color: black; font-weight: bold;'
                elif "Warning" in str(val):
                    return 'background-color: #ffe082; color: black;'
                return ''

            display_cols = ['drug_name', 'batch_no', 'quantity', 'mfg_date', 'expiry_date', 'days_until_expiry', 'status', 'voucher_id']
            st.dataframe(
                alerts_df[display_cols].style.applymap(highlight_expiry, subset=['status']),
                use_container_width=True
            )
        else:
            st.success("No items are currently approaching expiry within your set threshold.")
    else:
        st.info("No records found. Upload a PDF voucher in Tab 1 to get started.")

with tab3:
    st.subheader("Complete Stock & Batch Ledger")
    df_inv = fetch_inventory()
    if not df_inv.empty:
        st.dataframe(df_inv, use_container_width=True)
    else:
        st.info("Database is empty.")