import streamlit as st
import pandas as pd
import io
import os

# --- 1. APP CONFIG & DARK MODE ---
st.set_page_config(layout="wide", page_title="Fleet Command", page_icon="🚛")

# Force High-Contrast Black Theme
st.markdown("""
<style>
    /* Main Background */
    .stApp { background-color: #0e1117; }
    
    /* Text Colors */
    h1, h2, h3, h4, h5, h6 { color: #ffffff !important; }
    p, label, span, div { color: #e0e0e0; }
    
    /* Data Tables */
    .stDataFrame { border: 1px solid #333; }
    
    /* Metrics */
    div[data-testid="stMetricValue"] { color: #00ff41 !important; font-family: 'Courier New', monospace; }
    
    /* Truck Card Styling */
    .truck-card {
        background-color: #1c1c1c;
        border: 1px solid #333;
        border-radius: 8px;
        padding: 15px;
        margin-bottom: 10px;
    }
    .tag-KEEP { background-color: #006400; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
    .tag-SELL { background-color: #8b0000; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
    .tag-INSPECT { background-color: #b8860b; color: black; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# --- 2. ROBUST DATA LOADING ---
@st.cache_data
def load_initial_data():
    files = {
        'finance': 'truck-finance.xlsx',
        'repairs': 'maintenancepo-truck.xlsx',
        'distance': 'vehicle-distance-traveled.xlsx',
        'odometer': 'truck-odometer-data-week-.xlsx',
        'market': 'truck-paper.xlsx'
    }
    
    data = {}
    for role, filename in files.items():
        if os.path.exists(filename):
            try:
                # Load Excel or CSV
                if filename.endswith('.csv'):
                    df = pd.read_csv(filename)
                else:
                    # Smart Sheet Loader (Ignore "About" sheets)
                    xl = pd.ExcelFile(filename)
                    # Find a sheet that is NOT "About"
                    target_sheet = next((s for s in xl.sheet_names if 'about' not in s.lower()), xl.sheet_names[0])
                    df = xl.parse(target_sheet)

                # Fix Headers if row 0 is junk (common in exports)
                if "unnamed" in str(df.columns[0]).lower():
                    if filename.endswith('.csv'): df = pd.read_csv(filename, header=1)
                    else: df = xl.parse(target_sheet, header=1)

                # Standardize ID Column
                # We look for 'unit_id', 'TRUCK', or similar
                id_col = next((c for c in df.columns if 'unit' in str(c).lower() or 'truck' in str(c).lower() and 'price' not in str(c).lower()), None)
                if id_col:
                    # Clean the ID to match across files
                    df['clean_id'] = df[id_col].astype(str).str.replace('SPOT-', '').str.strip()
                
                data[role] = df
            except Exception as e:
                st.error(f"Error loading {filename}: {e}")
                data[role] = pd.DataFrame()
        else:
            data[role] = pd.DataFrame()
    return data

# Initialize Session State (This allows Editing)
if 'dfs' not in st.session_state:
    st.session_state['dfs'] = load_initial_data()

# --- 3. CALCULATION ENGINE ---
def run_logic(dfs):
    df_fin = dfs.get('finance', pd.DataFrame())
    df_rep = dfs.get('repairs', pd.DataFrame())
    df_odo = dfs.get('odometer', pd.DataFrame())
    df_dist = dfs.get('distance', pd.DataFrame())

    if df_fin.empty or 'clean_id' not in df_fin.columns:
        return pd.DataFrame()

    # 1. Base (Finance)
    # Estimate Payoff Balance
    pay_col = next((c for c in df_fin.columns if 'pay' in c.lower() and 'month' in c.lower()), None)
    if pay_col:
        df_fin['payoff_balance'] = pd.to_numeric(df_fin[pay_col], errors='coerce').fillna(0) * 12
    else:
        df_fin['payoff_balance'] = 0
        
    master = df_fin[['clean_id', 'payoff_balance']].copy()
    
    # Meta (Year/Make)
    for c in ['make', 'model', 'year']:
        col = next((x for x in df_fin.columns if c in x.lower()), None)
        master[c] = df_fin[col] if col else "N/A"

    # 2. Repairs
    if not df_rep.empty and 'clean_id' in df_rep.columns:
        amt_col = next((c for c in df_rep.columns if 'amount' in c.lower()), None)
        if amt_col:
            stats = df_rep.groupby('clean_id')[amt_col].sum().reset_index().rename(columns={amt_col: 'total_repairs'})
            master = master.merge(stats, on='clean_id', how='left')

    # 3. Odometer
    if not df_odo.empty and 'clean_id' in df_odo.columns:
        odo_col = next((c for c in df_odo.columns if 'odo' in c.lower()), None)
        if odo_col:
            stats = df_odo.groupby('clean_id')[odo_col].max().reset_index().rename(columns={odo_col: 'odometer'})
            master = master.merge(stats, on='clean_id', how='left')

    # 4. Distance
    if not df_dist.empty and 'clean_id' in df_dist.columns:
        dist_col = next((c for c in df_dist.columns if 'dist' in c.lower()), None)
        if dist_col:
            stats = df_dist.groupby('clean_id')[dist_col].sum().reset_index().rename(columns={dist_col: 'recent_miles'})
            master = master.merge(stats, on='clean_id', how='left')

    master = master.fillna(0)

    # 5. Formulas
    # Depreciation Logic
    master['est_resale'] = 65000 - (master['odometer'] * 0.05)
    master['est_resale'] = master['est_resale'].clip(lower=10000)
    
    # Equity
    master['net_equity'] = master['est_resale'] - master['payoff_balance']
    
    # CPM (Cost Per Mile)
    master['cpm'] = master['total_repairs'] / master['recent_miles'].replace(0, 1)

    # 6. Recommendation Logic
    def get_rec(row):
        reasons = []
        rec = "INSPECT"
        if row['odometer'] > 500000: reasons.append("High Miles")
        if row['cpm'] > 0.15: reasons.append(f"High CPM (${row['cpm']:.2f})")
        if row['net_equity'] > 0: reasons.append(f"Pos Equity")
        else: reasons.append("Neg Equity")
        
        if (row['odometer'] > 500000 or row['cpm'] > 0.15) and row['net_equity'] > 0:
            rec = "SELL"
        elif row['cpm'] < 0.12 and row['recent_miles'] > 2000:
            rec = "KEEP"
        elif row['recent_miles'] < 1000 and row['net_equity'] < 0:
            rec = "INSPECT"
            
        return pd.Series([rec, ", ".join(reasons)])

    res = master.apply(get_rec, axis=1)
    master['Action'] = res[0]
    master['Reasoning'] = res[1]
    
    return master

# --- 4. UI STRUCTURE ---
st.title("🚛 Fleet Command Center")

tab_dash, tab_entry, tab_export = st.tabs(["📊 Dashboard", "✏️ Data Entry (Add Trucks)", "💾 Export"])

# Calculate Data
master_df = run_logic(st.session_state['dfs'])

with tab_dash:
    if master_df.empty:
        st.warning("No data found. Please ensure 'truck-finance.xlsx' is in the folder or add trucks in Data Entry.")
    else:
        # Filters
        col1, col2 = st.columns([1,4])
        with col1:
            filter_opt = st.selectbox("Filter Action:", ["All", "KEEP", "SELL", "INSPECT"])
        
        view_df = master_df if filter_opt == "All" else master_df[master_df['Action'] == filter_opt]
        
        # Metrics
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Trucks", len(view_df))
        c2.metric("Total Equity", f"${view_df['net_equity'].sum():,.0f}")
        c3.metric("Avg Odometer", f"{view_df['odometer'].mean():,.0f}")
        c4.metric("Avg CPM", f"${view_df['cpm'].mean():.2f}")
        
        st.markdown("---")
        
        # Grid View
        for _, row in view_df.iterrows():
            st.markdown(f"""
            <div class="truck-card">
                <div style="display:flex; justify-content:space-between;">
                    <h3 style="margin:0;">{row['clean_id']} <span style="font-size:16px; color:#888;">({row['year']} {row['make']})</span></h3>
                    <span class="tag-{row['Action']}">{row['Action']}</span>
                </div>
                <p style="margin: 5px 0;">{row['Reasoning']}</p>
                <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:10px; margin-top:10px; font-size:14px;">
                    <div><span style="color:#888;">Equity:</span> <b style="color:#fff;">${row['net_equity']:,.0f}</b></div>
                    <div><span style="color:#888;">Resale:</span> <b style="color:#fff;">${row['est_resale']:,.0f}</b></div>
                    <div><span style="color:#888;">CPM:</span> <b style="color:#fff;">${row['cpm']:.2f}</b></div>
                    <div><span style="color:#888;">Miles:</span> <b style="color:#fff;">{row['odometer']:,.0f}</b></div>
                </div>
            </div>
            """, unsafe_allow_html=True)

with tab_entry:
    st.markdown("### 📝 Edit Master Data")
    st.info("Edit the table below to Add New Trucks or Update Finance details. Changes are saved automatically in this session.")
    
    # Editable Finance Table
    if 'finance' in st.session_state['dfs'] and not st.session_state['dfs']['finance'].empty:
        edited_df = st.data_editor(st.session_state['dfs']['finance'], num_rows="dynamic", use_container_width=True)
        st.session_state['dfs']['finance'] = edited_df
    else:
        st.error("Finance data not loaded. Please check file name.")

with tab_export:
    st.markdown("### 💾 Export Reports")
    
    # 1. Master Report
    if not master_df.empty:
        csv = master_df.to_csv(index=False).encode('utf-8')
        st.download_button("Download Decision Report (CSV)", csv, "truck_decisions.csv", "text/csv")
    
    # 2. Updated Excel
    st.divider()
    st.write("Download Updated Source File (with your new entries):")
    
    buffer = io.BytesIO()
    if 'finance' in st.session_state['dfs']:
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            st.session_state['dfs']['finance'].to_excel(writer, sheet_name='Data', index=False)
        
        st.download_button(
            label="Download Updated Finance.xlsx",
            data=buffer.getvalue(),
            file_name="truck-finance_updated.xlsx",
            mime="application/vnd.ms-excel"
        )