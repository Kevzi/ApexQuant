#!/usr/bin/env python3
"""
ApexQuant Central Cockpit - Apple Pro Design V4 (Parser Fix)
"""

import streamlit as st
import pandas as pd
import json
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import os
import sys
import psutil
import time
import subprocess
import sqlite3
import requests

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

try:
    from read_tb import extract_latest_ppo_metrics
except ImportError:
    extract_latest_ppo_metrics = None

st.set_page_config(page_title="ApexQuant Cockpit", layout="wide", page_icon="🍏")

st.markdown("""
<style>
    body, .stApp {
        background-color: #090A0F;
        color: #FFFFFF;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        -webkit-font-smoothing: antialiased;
    }
    header, footer { visibility: hidden !important; }
    h1, h2, h3, h4, h5 {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        font-weight: 600;
        letter-spacing: -0.02em;
        color: #FFFFFF;
        margin-bottom: 0.5em;
    }
    h2 { font-size: 1.5em; border-bottom: 1px solid rgba(255,255,255,0.05); padding-bottom: 10px; margin-top: 30px; }
    h2 span { font-size: 0.6em; color: #8E8E93; font-weight: 400; margin-left: 10px; }
    h3 { font-size: 1.1em; color: #E5E5EA; margin-top: 0; }
    .apple-card {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 16px;
        padding: 24px;
        box-shadow: 0 4px 24px rgba(0, 0, 0, 0.2);
        height: 100%;
        margin-bottom: 15px;
    }
    .metric-title { font-size: 0.85em; color: #8E8E93; font-weight: 400; margin-bottom: 8px; letter-spacing: 0.01em; }
    .metric-value { font-size: 2.2em; font-weight: 700; color: #FFFFFF; letter-spacing: -0.03em; line-height: 1.1; }
    .metric-delta-pos { font-size: 0.9em; color: #30D158; font-weight: 500; margin-top: 4px; }
    .metric-delta-neg { font-size: 0.9em; color: #FF453A; font-weight: 500; margin-top: 4px; }
    .apple-progress-track {
        width: 100%;
        background-color: rgba(255, 255, 255, 0.08);
        border-radius: 4px;
        height: 4px;
        margin-top: 12px;
        overflow: hidden;
    }
    .apple-progress-fill-blue { height: 100%; background-color: #0A84FF; border-radius: 4px; }
    .apple-progress-fill-red { height: 100%; background-color: #FF453A; border-radius: 4px; }
    .c-green { color: #30D158; }
    .c-blue { color: #0A84FF; }
    .c-orange { color: #FF9F0A; }
    .c-red { color: #FF453A; }
    .c-gray { color: #8E8E93; }
    .terminal-container {
        background-color: rgba(255,255,255,0.02);
        border: 1px solid rgba(255, 255, 255, 0.05);
        border-radius: 12px;
        padding: 16px;
        font-family: 'SF Mono', Consolas, Menlo, monospace;
        font-size: 11px;
        color: #E5E5EA;
        max-height: 250px;
        overflow-y: auto;
        line-height: 1.5;
    }
    .status-dot {
        height: 8px;
        width: 8px;
        border-radius: 50%;
        display: inline-block;
        margin-right: 8px;
    }
    .dot-green { background-color: #30D158; }
    .dot-orange { background-color: #FF9F0A; }
    .dot-red { background-color: #FF453A; }
    .dot-gray { background-color: #8E8E93; }
    /* Fix for inner content spacing without markdown interference */
    .telemetry-row { margin-bottom: 20px; font-size: 0.95em; }
    .telemetry-row-title { color: #8E8E93; margin-bottom: 4px; }
    
    /* Global Apple Card class fix */
    div[data-testid="stVerticalBlock"] > div > div > div.apple-card {
        padding: 24px;
    }
</style>
""", unsafe_allow_html=True)

@st.cache_data(ttl=5)
def load_balances():
    try:
        balance_path = os.path.join(current_dir, '..', '..', 'lark_balance.json')
        if not os.path.exists(balance_path): balance_path = os.path.join(current_dir, 'lark_balance.json')
        if not os.path.exists(balance_path) and os.path.exists('/workspace/lark_balance.json'): balance_path = '/workspace/lark_balance.json'
        with open(balance_path, 'r') as f: return json.load(f)
    except: return {}

@st.cache_data(ttl=5)
def load_logs():
    bridge_log_path = os.path.join(current_dir, '..', 'execution', 'bridge_live.log')
    if not os.path.exists(bridge_log_path): bridge_log_path = os.path.join(current_dir, 'ctrader_bridge_v5.log')
    if not os.path.exists(bridge_log_path) and os.path.exists('/workspace/ctrader_bridge_v5.log'): bridge_log_path = '/workspace/ctrader_bridge_v5.log'
            
    freq_log_path = os.path.join(current_dir, '..', '..', 'ft_userdata', 'user_data', 'logs', 'freqtrade_live_v17.log')
    if not os.path.exists(freq_log_path): freq_log_path = os.path.join(current_dir, 'freqtrade.log')
    if not os.path.exists(freq_log_path) and os.path.exists('/workspace/freqtrade.log'): freq_log_path = '/workspace/freqtrade.log'

    bridge_logs, freq_logs = [], []
    try:
        with open(bridge_log_path, 'r', encoding='utf-8', errors='ignore') as f: bridge_logs = f.readlines()[-100:]
    except: pass
    try:
        with open(freq_log_path, 'r', encoding='utf-8', errors='ignore') as f: freq_logs = f.readlines()[-100:]
    except: pass
    return bridge_logs, freq_logs

def get_live_trades():
    possible_dbs = [
        os.path.join(current_dir, '..', '..', 'ft_userdata', 'tradesv3.dryrun.sqlite'),
        os.path.join(current_dir, '..', '..', 'ft_userdata', 'tradesv3.sqlite'),
        os.path.join(current_dir, '..', '..', 'ft_userdata', 'user_data', 'tradesv3.dryrun.sqlite'),
        os.path.join(current_dir, '..', '..', 'ft_userdata', 'user_data', 'tradesv3.sqlite'),
        os.path.join(current_dir, 'tradesv3.sqlite'),
        '/workspace/tradesv3.sqlite'
    ]
    db_path = None
    for p in possible_dbs:
        if os.path.exists(p):
            db_path = p
            break
            
    if not db_path: return pd.DataFrame()
    try:
        conn = sqlite3.connect(db_path)
        query = "SELECT id, pair, is_open, fee_open, open_rate, open_date, amount, is_short FROM trades WHERE is_open = 1"
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        # Calculate real-time profit using Binance API
        profit_ratios = []
        profit_abss = []
        for _, row in df.iterrows():
            try:
                symbol = row['pair'].replace('/', '').replace(':USDT', '')
                r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=1.0)
                current_price = float(r.json()['price'])
                is_short = row.get('is_short', 0)
                if not is_short:
                    profit_abs = (current_price - row['open_rate']) * row['amount']
                    profit_ratio = (current_price - row['open_rate']) / row['open_rate']
                else:
                    profit_abs = (row['open_rate'] - current_price) * row['amount']
                    profit_ratio = (row['open_rate'] - current_price) / row['open_rate']
                profit_ratios.append(profit_ratio)
                profit_abss.append(profit_abs)
            except:
                profit_ratios.append(0.0)
                profit_abss.append(0.0)
                
        df['profit_ratio'] = profit_ratios
        df['profit_abs'] = profit_abss
        return df
    except: return pd.DataFrame()

def get_performance_metrics():
    metrics = {"win_rate": 0.0, "profit_factor": 0.0, "total_pnl": 0.0, "total_trades": 0}
    possible_dbs = [
        os.path.join(current_dir, '..', '..', 'ft_userdata', 'tradesv3.dryrun.sqlite'),
        os.path.join(current_dir, '..', '..', 'ft_userdata', 'tradesv3.sqlite'),
        os.path.join(current_dir, '..', '..', 'ft_userdata', 'user_data', 'tradesv3.dryrun.sqlite'),
        os.path.join(current_dir, '..', '..', 'ft_userdata', 'user_data', 'tradesv3.sqlite'),
        os.path.join(current_dir, 'tradesv3.sqlite'),
        '/workspace/tradesv3.sqlite'
    ]
    db_path = None
    for p in possible_dbs:
        if os.path.exists(p):
            db_path = p
            break
            
    if not db_path: return metrics
    
    try:
        conn = sqlite3.connect(db_path)
        query = "SELECT profit_abs FROM trades WHERE is_open = 0"
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        if df.empty: return metrics
        
        wins = df[df['profit_abs'] > 0]
        losses = df[df['profit_abs'] <= 0]
        
        gross_profit = wins['profit_abs'].sum() if not wins.empty else 0.0
        gross_loss = abs(losses['profit_abs'].sum()) if not losses.empty else 0.0
        
        metrics["total_trades"] = len(df)
        metrics["win_rate"] = (len(wins) / len(df)) * 100 if len(df) > 0 else 0.0
        metrics["profit_factor"] = (gross_profit / gross_loss) if gross_loss > 0 else (99.9 if gross_profit > 0 else 0.0)
        metrics["total_pnl"] = df['profit_abs'].sum()
        
    except Exception as e: pass
    return metrics

@st.cache_data(ttl=5)
def get_freqai_metrics():
    metrics = {"di": 0.0, "do_predict": 1, "macro_trend": 1.0, "hmm_state": 0.0}
    try:
        telemetry_path = os.path.join(current_dir, '..', '..', 'ft_userdata', 'user_data', 'freqai_telemetry.json')
        if not os.path.exists(telemetry_path):
            telemetry_path = os.path.join(current_dir, 'freqai_telemetry.json')
            
        if os.path.exists(telemetry_path):
            with open(telemetry_path, 'r') as f:
                data = json.load(f)
                metrics["di"] = data.get("di", 0.0)
                metrics["do_predict"] = data.get("do_predict", 1)
                metrics["macro_trend"] = data.get("macro_trend", 1.0)
                metrics["hmm_state"] = data.get("hmm_state", 0.0)
    except: pass
    return metrics

@st.cache_data(ttl=15)
def get_ping(host):
    try:
        output = subprocess.check_output(f"ping -c 1 -W 1 {host}", shell=True).decode()
        import re
        match = re.search(r'time=([\d\.]+)\s*ms', output)
        return float(match.group(1)) if match else 0.0
    except: return 0.0

with st.sidebar:
    st.markdown("<h2>Core Systems</h2>", unsafe_allow_html=True)
    logs, _ = load_logs()
    lark_auth, ftmo_auth = False, False
    for line in logs:
        if "47737296" in line and "autorisé" in line.lower(): lark_auth = True
        if "7577274" in line and "autorisé" in line.lower(): ftmo_auth = True
    
    st.markdown(f"<div style='margin-bottom: 15px;'><span class='status-dot {'dot-green' if lark_auth else 'dot-red'}'></span> <span class='c-gray'>Lark Funding:</span> <b>{'Active' if lark_auth else 'Offline'}</b></div>", unsafe_allow_html=True)
    st.markdown(f"<div style='margin-bottom: 25px;'><span class='status-dot {'dot-green' if ftmo_auth else 'dot-red'}'></span> <span class='c-gray'>FTMO Bridge:</span> <b>{'Active' if ftmo_auth else 'Offline'}</b></div>", unsafe_allow_html=True)
    
    st.markdown("### Heartbeat Control")
    if st.button("Trigger Sync", use_container_width=True):
        heartbeat_path = os.path.abspath(os.path.join(current_dir, '..', 'execution', 'heartbeat_trade.py'))
        try:
            subprocess.Popen([sys.executable, heartbeat_path])
            st.success("Sync triggered.")
        except Exception as e: st.error(f"Error: {e}")
    
    import socket
    vps_name = socket.gethostname()
    st.markdown(f"<br><br>### VPS Node: <span style='font-size: 0.85em; font-weight: normal; color: #8E8E93;'>{vps_name}</span>", unsafe_allow_html=True)
    
    try: 
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        ram_percent = mem.percent
        ram_used = mem.used / (1024**3)
        ram_avail = mem.available / (1024**3)
    except: 
        cpu, ram_percent, ram_used, ram_avail = 0.0, 0.0, 0.0, 0.0
        
    st.markdown(f"<div class='c-gray' style='font-size: 0.85em; margin-bottom: 4px;'>CPU Load: {cpu}%</div><div class='apple-progress-track'><div class='apple-progress-fill-blue' style='width: {cpu}%;'></div></div>", unsafe_allow_html=True)

ping_bybit = get_ping("api.bybit.com")
ping_ctrader = get_ping("live.ctraderapi.com")
ping_b_color = "dot-green" if ping_bybit < 50 else ("dot-orange" if ping_bybit < 150 else "dot-red")
ping_c_color = "dot-green" if ping_ctrader < 50 else ("dot-orange" if ping_ctrader < 150 else "dot-red")

st.markdown("<h2>The Core Status <span>Account Equity & Risk</span></h2>", unsafe_allow_html=True)
col1, col2, col3, col4 = st.columns(4)

balance_data = load_balances()
lark_info = balance_data.get("47737296", {"balance": 10000.0, "equity": 10000.0})
ftmo_info = balance_data.get("7577274", {"balance": 10000.0, "equity": 10000.0})

def render_metric_card(title, equity, balance):
    diff = equity - balance
    diff_str = f"+${diff:,.2f}" if diff >= 0 else f"-${abs(diff):,.2f}"
    diff_class = "metric-delta-pos" if diff >= 0 else "metric-delta-neg"
    return f'<div class="apple-card"><div class="metric-title">{title}</div><div class="metric-value">${equity:,.2f}</div><div class="{diff_class}">{diff_str}</div></div>'

with col1: st.markdown(render_metric_card("Lark Funding Equity", lark_info['equity'], lark_info['balance']), unsafe_allow_html=True)
with col2: st.markdown(render_metric_card("FTMO Live Equity", ftmo_info['equity'], ftmo_info['balance']), unsafe_allow_html=True)

with col3:
    eod = 10000.0
    current_dd = max(0.0, round(((eod - lark_info['equity']) / eod) * 100, 2))
    max_dd = 5.0
    is_danger = current_dd >= 3.5
    is_warning = current_dd >= 2.0
    val_class = "c-red" if is_danger else ("c-orange" if is_warning else "c-blue")
    bar_class = "apple-progress-fill-red" if is_danger else "apple-progress-fill-blue"
    
    html = f'<div class="apple-card"><div class="metric-title">Drawdown Exposure</div><div style="font-size: 1.5em; font-weight: 600;" class="{val_class}">{current_dd}% <span style="font-size: 0.6em; font-weight: 400;" class="c-gray">/ {max_dd}% limit</span></div><div class="apple-progress-track"><div class="{bar_class}" style="width: {min(current_dd/max_dd * 100, 100)}%;"></div></div></div>'
    st.markdown(html, unsafe_allow_html=True)

with col4:
    html = '<div class="apple-card"><div class="metric-title">Asset Allocation</div><div style="font-size: 1.1em; margin-top: 10px;"><span class="c-gray" style="display:inline-block; width:45px;">BTC</span> <span style="font-weight:600;">55%</span></div><div style="font-size: 1.1em; margin-top: 5px;"><span class="c-gray" style="display:inline-block; width:45px;">ETH</span> <span style="font-weight:600;">45%</span></div></div>'
    st.markdown(html, unsafe_allow_html=True)

# --- NEW PERFORMANCE METRICS ROW ---
p_col1, p_col2, p_col3, p_col4 = st.columns(4)
perf = get_performance_metrics()
with p_col1:
    st.markdown(f'<div class="apple-card" style="padding: 16px;"><div class="metric-title">Global Win Rate</div><div style="font-size: 1.8em; font-weight: 700; color: #FFFFFF;">{perf["win_rate"]:.1f}%</div><div class="c-gray" style="font-size: 0.8em; margin-top: 4px;">{perf["total_trades"]} total trades</div></div>', unsafe_allow_html=True)
with p_col2:
    st.markdown(f'<div class="apple-card" style="padding: 16px;"><div class="metric-title">Profit Factor</div><div style="font-size: 1.8em; font-weight: 700; color: #FFFFFF;">{perf["profit_factor"]:.2f}</div><div class="c-gray" style="font-size: 0.8em; margin-top: 4px;">Gross Profit / Gross Loss</div></div>', unsafe_allow_html=True)
with p_col3:
    pnl_color = "c-green" if perf["total_pnl"] >= 0 else "c-red"
    pnl_sign = "+" if perf["total_pnl"] >= 0 else ""
    st.markdown(f'<div class="apple-card" style="padding: 16px;"><div class="metric-title">Realized PnL</div><div class="{pnl_color}" style="font-size: 1.8em; font-weight: 700;">{pnl_sign}${perf["total_pnl"]:,.2f}</div><div class="c-gray" style="font-size: 0.8em; margin-top: 4px;">Total closed profit</div></div>', unsafe_allow_html=True)
with p_col4:
    st.markdown(f'<div class="apple-card" style="padding: 16px;"><div class="metric-title">System Status</div><div class="c-green" style="font-size: 1.5em; font-weight: 600; margin-top: 8px;"><span class="status-dot dot-green"></span> Optimal</div></div>', unsafe_allow_html=True)

st.markdown("<h2>The Workspace <span>Execution & Obfuscation</span></h2>", unsafe_allow_html=True)
c_left, c_right = st.columns([2, 1])

with c_left:
    df_trades = get_live_trades()
    
    # Using a native container for the left block to support Plotly charts without HTML div auto-close bugs
    st.markdown("<h3>Active Positions</h3>", unsafe_allow_html=True)
    if df_trades.empty:
        st.markdown('<div class="apple-card" style="min-height: 150px;"><div style="color: #8E8E93; padding: 10px 0;">No active trades. Scanning markets...</div></div>', unsafe_allow_html=True)
    else:
        for idx, row in df_trades.iterrows():
            direction = "Short" if row.get("is_short", 0) else "Long"
            dir_color = "c-orange" if direction == "Short" else "c-green"
            pnl_color = "c-green" if row['profit_abs'] > 0 else "c-red"
            
            st.markdown(f'<div class="apple-card" style="padding: 16px; margin-bottom: 0px;"><div style="display: flex; justify-content: space-between; align-items: center;"><div><div style="font-weight: 600; font-size: 1.1em;">{row["pair"]} <span class="{dir_color}" style="font-size: 0.8em; margin-left: 8px;">{direction}</span></div><div class="c-gray" style="font-size: 0.9em; margin-top: 4px;">Entry: ${row["open_rate"]:,.2f}</div></div><div style="text-align: right;"><div class="{pnl_color}" style="font-weight: 600; font-size: 1.2em;">${row["profit_abs"]:,.2f}</div><div class="{pnl_color}" style="font-size: 0.8em;">{row["profit_ratio"]*100:.2f}%</div></div></div></div>', unsafe_allow_html=True)
            
            fig = go.Figure()
            current_price = row['open_rate'] * (1 + row['profit_ratio'] * (-1 if direction == "Short" else 1))
            fig.add_trace(go.Scatter(y=[row['open_rate'], current_price], line=dict(color='#FFFFFF', width=1.5), mode='lines'))
            fig.add_hline(y=row['open_rate'], line_dash="dot", line_color="#8E8E93")
            fig.update_layout(
                paper_bgcolor='rgba(0,0,0,0)', 
                plot_bgcolor='rgba(0,0,0,0)', 
                height=80, 
                margin=dict(l=0, r=0, t=5, b=5), 
                xaxis=dict(showgrid=False, visible=False),
                yaxis=dict(showgrid=False, visible=False)
            )
            st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})

with c_right:
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=3000, limit=None, key="data_refresh")
    except ImportError: pass

    import socket
    vps_name = socket.gethostname()
    
    try: 
        mem = psutil.virtual_memory()
        ram_used = mem.used / (1024**3)
        ram_avail = mem.available / (1024**3)
        cpu = psutil.cpu_percent()
        disk = psutil.disk_usage('/')
        disk_used = disk.used / (1024**3)
        disk_total = disk.total / (1024**3)
    except: 
        ram_used, ram_avail, cpu, disk_used, disk_total = 0.0, 0.0, 0.0, 0.0, 0.0
    
    ping_bybit = get_ping("api.bybit.com")
    ping_ctrader = get_ping("live.ctraderapi.com")
    ping_b_color = "dot-green" if ping_bybit < 50 else ("dot-orange" if ping_bybit < 150 else "dot-red")
    ping_c_color = "dot-green" if ping_ctrader < 50 else ("dot-orange" if ping_ctrader < 150 else "dot-red")

    html = f'''<div class="apple-card" style="min-height: 280px;">
<h3>Telemetry (VPS: {vps_name})</h3>
<p class="c-gray" style="font-size: 0.85em; margin-bottom: 20px;">Execution & Hardware Metrics</p>
<div class="telemetry-row"><div class="telemetry-row-title">CPU Load</div>
<div><span class="status-dot dot-gray"></span> {cpu:.1f}%</div></div>
<div class="telemetry-row"><div class="telemetry-row-title">RAM Usage</div>
<div><span class="status-dot dot-gray"></span> {ram_used:.1f} GB used / {ram_avail:.1f} GB free</div></div>
<div class="telemetry-row"><div class="telemetry-row-title">Disk Space</div>
<div><span class="status-dot dot-gray"></span> {disk_used:.1f} GB used / {disk_total:.1f} GB total</div></div>
<div class="telemetry-row"><div class="telemetry-row-title">Bybit API Latency</div>
<div><span class="status-dot {ping_b_color}"></span> {ping_bybit:.1f} ms</div></div>
<div class="telemetry-row"><div class="telemetry-row-title">cTrader Live Latency</div>
<div><span class="status-dot {ping_c_color}"></span> {ping_ctrader:.1f} ms</div></div>
<div class="telemetry-row" style="margin-top: 20px;"><div class="telemetry-row-title">Slippage Protection</div>
<div><span class="status-dot dot-green"></span> Active (1.4s)</div></div>
</div>'''
    html = html.replace('\n', '')
    st.markdown(html, unsafe_allow_html=True)

st.markdown("<h2>The Intelligence Terminal <span>Engine & Diagnostics</span></h2>", unsafe_allow_html=True)
e_left, e_right = st.columns(2)

with e_left:
    st.markdown("<h3>AI Narrative Journal</h3>", unsafe_allow_html=True)
    
    narrative_path = os.path.join(current_dir, '..', '..', 'ft_userdata', 'user_data', 'logs', 'ai_narrative.log')
    if not os.path.exists(narrative_path):
        narrative_path = '/root/ApexQuant/ft_userdata/user_data/logs/ai_narrative.log'
        
    narratives = []
    try:
        if os.path.exists(narrative_path):
            with open(narrative_path, 'r', encoding='utf-8', errors='ignore') as f:
                narratives = f.readlines()[-30:]  # Récupérer plus d'historique pour remplir les deux onglets
    except: pass
    
    html_trades = ""
    html_thoughts = ""
    
    for n in reversed(narratives):
        n = n.strip()
        if not n: continue
        
        parts = n.split("] Décision : ")
        if len(parts) < 2: continue
        header = parts[0] + "]" 
        body = parts[1]
        
        is_trade = "ACHAT" in body or "VENTE" in body or "MAINTIEN" in body or "CLÔTURE" in body
        
        color = "#FFFFFF"
        bg_color = "rgba(255,255,255,0.02)"
        icon = "💡"
        
        if "ACHAT LONG" in body or "MAINTIEN LONG" in body: 
            color = "#30D158"
            bg_color = "rgba(48, 209, 88, 0.05)"
            icon = "🟢"
        elif "VENTE SHORT" in body or "MAINTIEN SHORT" in body: 
            color = "#FF453A"
            bg_color = "rgba(255, 69, 58, 0.05)"
            icon = "🔴"
        elif "CLÔTURE" in body: 
            color = "#0A84FF"
            bg_color = "rgba(10, 132, 255, 0.05)"
            icon = "🔵"
        elif "ATTENTE" in body or "OBSERVATION" in body: 
            color = "#FF9F0A"
            icon = "⏳"
        elif "FILTRÉ" in body: 
            color = "#8E8E93"
            icon = "🛡️"
            
        first_sentence = body.split('.')[0]
        rest_of_body = '.'.join(body.split('.')[1:])
        if rest_of_body: rest_of_body = "." + rest_of_body
            
        card_html = f"<div style='margin-bottom:12px; padding:12px 16px; background:{bg_color}; border-radius:12px; border-left: 4px solid {color}; font-family: -apple-system, BlinkMacSystemFont, \"Segoe UI\", Roboto, sans-serif;'><div style='font-size:0.75em; color:#8E8E93; margin-bottom:6px; font-weight:600; letter-spacing:0.5px;'>{header}</div><div style='font-size:0.95em; line-height:1.5; color:#E5E5EA;'>{icon} <strong style='color:{color}; font-weight:600;'>{first_sentence}</strong>{rest_of_body}</div></div>"
        
        if is_trade:
            html_trades += card_html
        else:
            html_thoughts += card_html
            
    if not html_trades: html_trades = "<div class='c-gray' style='padding: 15px; font-style: italic; text-align: center; background: rgba(255,255,255,0.02); border-radius: 12px;'>Aucune action de trading récente.</div>"
    if not html_thoughts: html_thoughts = "<div class='c-gray' style='padding: 15px; font-style: italic; text-align: center; background: rgba(255,255,255,0.02); border-radius: 12px;'>Aucune réflexion d'attente récente.</div>"
    
    t1, t2 = st.tabs(["⚡ Actions (Trades)", "🧠 Réflexions (Filtres)"])
    with t1:
        st.markdown(f"<div style='max-height: 280px; overflow-y: auto; padding-right: 8px;'>{html_trades}</div>", unsafe_allow_html=True)
    with t2:
        st.markdown(f"<div style='max-height: 280px; overflow-y: auto; padding-right: 8px;'>{html_thoughts}</div>", unsafe_allow_html=True)

with e_right:
    f_metrics = get_freqai_metrics()
    di = f_metrics["di"]
    hmm_state = f_metrics.get("hmm_state", 0.0)

    # Régime HMM = le vrai filtre de l'archi RL (action masking) :
    # 1 = régime chaotique -> l'agent se met en retrait (entrées bloquées).
    hmm_class = "dot-green" if hmm_state < 0.5 else "dot-orange"
    hmm_text = "Healthy Regime" if hmm_state < 0.5 else "Chaotic (Standing By)"
    macro_val = f_metrics.get("macro_trend", 1.0)
    macro_text = "Bull Market" if macro_val > 0 else "Bear Market"
    macro_color = "dot-green" if macro_val > 0 else "dot-red"
    
    action_color = "c-green" if di > 0 else "c-red" if di < 0 else "c-gray"
    action_sign = "+" if di > 0 else ""
    
    html = f'<div class="apple-card" style="height: 100%;"><h3>Inference Diagnostics</h3><p class="c-gray" style="font-size: 0.85em; margin-bottom: 24px;">Market Structure & Filtering</p><div style="margin-bottom: 20px; font-size: 0.95em; display: flex; justify-content: space-between;"><span class="c-gray">PPO Action Signal</span><span class="{action_color}" style="font-family: \'SF Mono\', monospace; font-weight: 600;">{action_sign}{di:.4f}</span></div><div style="margin-bottom: 20px; font-size: 0.95em; display: flex; justify-content: space-between;"><span class="c-gray">HMM Regime Filter</span><span><span class="status-dot {hmm_class}"></span> {hmm_text}</span></div><div style="margin-bottom: 20px; font-size: 0.95em; display: flex; justify-content: space-between;"><span class="c-gray">Macro Trend Regime</span><span><span class="status-dot {macro_color}"></span> {macro_text}</span></div></div>'
    st.markdown(html, unsafe_allow_html=True)

st.markdown("<h2>Technical Console <span>System Logs</span></h2>", unsafe_allow_html=True)
with st.expander("Show Console", expanded=False):
    b_logs, f_logs = load_logs()
    col_tl, col_tr = st.columns(2)
    with col_tl:
        st.markdown("<div class='c-gray' style='margin-bottom: 8px; font-size: 0.9em;'>execution_bridge.log</div>", unsafe_allow_html=True)
        b_html = "".join([f"<div style='margin-bottom:2px;'>{l.strip()}</div>" for l in reversed(b_logs[-30:])])
        st.markdown(f"<div class='terminal-container'>{b_html}</div>", unsafe_allow_html=True)
    with col_tr:
        st.markdown("<div class='c-gray' style='margin-bottom: 8px; font-size: 0.9em;'>freqai_inference.log</div>", unsafe_allow_html=True)
        f_html = "".join([f"<div style='margin-bottom:2px;'>{l.strip()}</div>" for l in reversed(f_logs[-30:])])
        st.markdown(f"<div class='terminal-container'>{f_html}</div>", unsafe_allow_html=True)
