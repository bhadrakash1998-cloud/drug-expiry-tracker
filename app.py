import streamlit as st
import pdfplumber
import re
import pandas as pd
from datetime import datetime, date
import calendar
from sqlalchemy import create_engine, text
import io

# ---------------------------------------------------------
# DATABASE SETUP (SQLAlchemy for Supabase / PostgreSQL)
# ---------------------------------------------------------
if "postgres" in st.secrets:
    db_url = st.secrets["postgres"]["url"]
elif "postgres_url" in st.secrets:
    db_url = st.secrets["postgres_url"]
else:
    db_url = "sqlite:///inventory.db"

engine = create_engine(db_url)

def init_db():
    with engine.connect() as conn:
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS inventory (
                id SERIAL PRIMARY KEY,
                voucher_id TEXT,
                drug_name TEXT,
                batch_no TEXT,
                mfg_date TEXT,
                expiry_date TEXT,
                quantity INTEGER,
                upload_date TEXT
            )
        '''))
        conn.commit()

def save_items_to_db(voucher_id, items):
    upload_date = date.today().isoformat()
    with engine.connect() as conn:
        for item in items:
            conn.execute(text('''
                INSERT INTO inventory (voucher_id, drug_name, batch_no, mfg_date, expiry_date, quantity, upload_date)
                VALUES (:voucher_id, :drug_name, :batch_no, :mfg_date, :expiry_date, :quantity, :upload_date)
            '''), {
                'voucher_id': voucher_id,
                'drug_name': str(item.get('drug_name', 'Unknown')),
                'batch_no': str(item.get('batch_no', 'N/A')),
                'mfg_date': str(item.get('mfg_date', 'N/A')),
                'expiry_date': str(item.get('expiry_date', 'N/A')),
                'quantity': int(item.get('quantity', 0)) if str(item.get('quantity', '')).isdigit() else 0,
                'upload_date': upload_date
            })
        conn.commit()

def delete_item_by_id(item_id):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM inventory WHERE id = :id"), {"id": item_id})
        conn.commit()

def fetch_inventory():
    with engine.connect() as conn:
        df = pd.read_sql_query(text("SELECT * FROM inventory ORDER BY id DESC"), conn)
    return df

# ---------------------------------------------------------
# DVDMS / e-AUSHADHI PDF EXTRACTION ENGINE
# ---------------------------------------------------------
def parse_date(date_str):
    """Normalizes dates to standard YYYY-MM-DD format."""
    if not date_str or date_str == "N/A":
        return "N/A"
    formats = ["%d/%m/%Y", "%d-%m-%Y", "%m/%Y", "%m-%Y", "%Y-%m-%d", "%b-%Y", "%b/%Y"]
    clean_str = date_str.strip()
    for fmt in formats:
        try:
            dt = datetime.strptime(clean_str, fmt)
            if fmt in ["%m/%Y", "%m-%Y", "%b-%Y", "%b/%Y"]:
                _, last_day = calendar.monthrange(dt.year, dt.month)
                dt = dt.replace(day=last_day)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return clean_str

def extract_drug_data_from_pdf(pdf_file):
    """Robust parser across all pages for DVDMS / e-Aushadhi PDF Vouchers."""
    extracted_items = []
    
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 1:
                    continue
                
                header_idx = -1
                col_map = {"name": -1, "batch": -1, "expiry": -1, "qty": -1}
                
                # Dynamic header discovery per page/table
                for idx, row in enumerate(table[:6]):
                    row_text = [str(cell).lower().replace('\n', ' ') if cell else '' for cell in row]
                    joined_row = " ".join(row_text)
                    
                    if any(k in joined_row for k in ["item name", "drug name", "batch", "exp", "issued", "qty"]):
                        header_idx = idx
                        for c_i, cell in enumerate(row_text):
                            if any(k in cell for k in ["item name", "drug name", "drug/item", "item code", "particular"]):
                                col_map["name"] = c_i
                            elif "batch" in cell:
                                col_map["batch"] = c_i
                            elif any(k in cell for k in ["exp", "expiry"]):
                                col_map["expiry"] = c_i
                            elif any(k in cell for k in ["issued", "qty", "quantity", "rec", "in hand", "dispatched"]):
                                col_map["qty"] = c_i
                        break
                
                start_row = header_idx + 1 if header_idx != -1 else 0
                for row in table[start_row:]:
                    clean_row = [str(cell).strip().replace('\n', ' ') if cell else '' for cell in row]
                    joined_line = " ".join(clean_row).lower()
                    
                    if not any(clean_row) or "item name" in joined_line or "total" in joined_line or "page" in joined_line:
                        continue

                    drug_name = clean_row[col_map["name"]] if col_map["name"] != -1 and col_map["name"] < len(clean_row) else clean_row[0]
                    drug_name = re.sub(r'^\d+[\.\)]\s*', '', drug_name)
                    
                    batch_no = clean_row[col_map["batch"]] if col_map["batch"] != -1 and col_map["batch"] < len(clean_row) else "N/A"
                    if not batch_no or batch_no == "":
                        batch_no = "N/A"

                    exp_date = "N/A"
                    if col_map["expiry"] != -1 and col_map["expiry"] < len(clean_row):
                        exp_date = parse_date(clean_row[col_map["expiry"]])
                    
                    if exp_date == "N/A":
                        date_matches = [c for c in clean_row if re.search(r'\b(\d{1,2}[/-]\d{2,4}|\d{2,4}[/-]\d{1,2}|[A-Za-z]{3}[/-]\d{2,4})\b', c)]
                        if date_matches:
                            exp_date = parse_date(date_matches[-1])

                    qty = 0
                    if col_map["qty"] != -1 and col_map["qty"] < len(clean_row):
                        clean_qty = re.sub(r'[^\d]', '', clean_row[col_map["qty"]])
                        qty = int(clean_qty) if clean_qty.isdigit() else 0
                    else:
                        for cell in reversed(clean_row):
                            clean_qty = re.sub(r'[^\d]', '', cell)
                            if clean_qty.isdigit() and 0 < len(clean_qty) <= 7:
                                qty = int(clean_qty)
                                break
                    
                    if drug_name and len(drug_name) > 2 and not drug_name.lower().startswith("sub total"):
                        extracted_items.append({
                            "drug_name": drug_name,
                            "batch_no": batch_no,
                            "mfg_date": "N/A",
                            "expiry_date": exp_date,
                            "quantity": qty
                        })

    return extracted_items

# ---------------------------------------------------------
# STREAMLIT UI
# ---------------------------------------------------------
st.set_page_config(page_title="CMS Drug Expiry Tracker", layout="wide", page_icon="💊")

init_db()

st.title("💊 Central Medical Store - Drug Expiry Tracker")
st.markdown("Upload voucher copy PDFs to track stock batches and monitor impending drug expiries across all devices.")

st.sidebar.header("⚙️ Settings & Alert Rules")
warning_days = st.sidebar.slider("Warning Threshold (Days)", min_value=30, max_value=180, value=90, step=15)
critical_days = st.sidebar.slider("Critical Threshold (Days)", min_value=7, max_value=60, value=30, step=7)

tab1, tab2, tab3 = st.tabs(["📤 Upload Voucher PDF", "⚠️ Expiry Alerts & Status", "📦 Full Inventory Records"])

# --- TAB 1: UPLOAD & EDIT ---
with tab1:
    st.subheader("Upload Central Medical Store Voucher / Report")
    uploaded_file = st.file_uploader("Select a PDF voucher file", type=["pdf"])
    
    parsed_data = []
    if uploaded_file is not None:
        st.info("Parsing PDF content...")
        parsed_data = extract_drug_data_from_pdf(uploaded_file)
        if parsed_data:
            st.success(f"Successfully extracted {len(parsed_data)} items from PDF.")
        else:
            st.warning("Could not auto-extract table rows. If this is a scanned image PDF, you can review or enter items below.")

    st.divider()
    st.write("### 📝 Batch Items Ledger Entry")
    st.caption("Review extracted items or manually edit stock rows below before saving to cloud database.")
    
    initial_data = parsed_data if parsed_data else [{
        "drug_name": "Paracetamol 500mg", 
        "batch_no": "B1234", 
        "mfg_date": "N/A", 
        "expiry_date": "2027-12-31", 
        "quantity": 1000
    }]
    
    edited_df = st.data_editor(pd.DataFrame(initial_data), num_rows="dynamic", use_container_width=True)
    voucher_ref = st.text_input("Voucher / Invoice Reference No.", value=f"CHC-DIGLIPUR-{datetime.now().strftime('%Y%m%d%H%M')}")
    
    if st.button("💾 Save Stock to Central Database", type="primary"):
        if not edited_df.empty:
            save_items_to_db(voucher_ref, edited_df.to_dict('records'))
            st.success("All items successfully saved to Supabase database!")
            st.rerun()
        else:
            st.error("Please add at least one valid drug item row before saving.")

# --- TAB 2: ALERTS ---
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

            display_cols = ['id', 'drug_name', 'batch_no', 'quantity', 'mfg_date', 'expiry_date', 'days_until_expiry', 'status', 'voucher_id']
            st.dataframe(
                alerts_df[display_cols].style.map(highlight_expiry, subset=['status']),
                use_container_width=True
            )
        else:
            st.success("No items are currently approaching expiry within your set threshold.")
    else:
        st.info("No records found in database. Upload a PDF voucher in Tab 1 to get started.")

# --- TAB 3: FULL LEDGER, EXPORT & DELETE ---
with tab3:
    st.subheader("Complete Stock & Batch Ledger")
    df_inv = fetch_inventory()
    if not df_inv.empty:
        st.dataframe(df_inv, use_container_width=True)
        
        st.divider()
        col_exp, col_del = st.columns(2)
        
        # EXPORT SECTION
        with col_exp:
            st.subheader("📥 Export Reports")
            csv_data = df_inv.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📄 Download Inventory as CSV",
                data=csv_data,
                file_name=f"stock_ledger_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                use_container_width=True
            )
            
            try:
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    df_inv.to_excel(writer, index=False, sheet_name='Stock Ledger')
                excel_data = buffer.getvalue()
                
                st.download_button(
                    label="📊 Download Inventory as Excel (.xlsx)",
                    data=excel_data,
                    file_name=f"stock_ledger_{datetime.now().strftime('%Y%m%d')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )
            except Exception:
                st.info("Install openpyxl in requirements.txt to enable Excel downloads.")

        # DELETE SECTION
        with col_del:
            st.subheader("🗑️ Delete Inventory Record")
            st.caption("Select a specific duplicate or incorrect drug batch to permanently remove it from Supabase.")
            
            # Create dropdown options formatted as: "ID 12 | Paracetamol 500mg | Batch: B1234"
            item_options = {
                f"ID {row['id']} | {row['drug_name']} (Batch: {row['batch_no']}, Qty: {row['quantity']})": row['id']
                for _, row in df_inv.iterrows()
            }
            
            selected_label = st.selectbox("Select drug batch to delete:", options=list(item_options.keys()))
            selected_id = item_options[selected_label]
            
            if st.button("🗑️ Delete Selected Batch", type="secondary"):
                delete_item_by_id(selected_id)
                st.success(f"Successfully deleted Record ID {selected_id} from database!")
                st.rerun()

    else:
        st.info("Database is empty.")
