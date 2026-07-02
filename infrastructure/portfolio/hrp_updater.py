import sys
import os
import json
import requests
import pandas as pd
from datetime import datetime
import time

# Permet d'importer la fonction HRP locale
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
from hrp_allocation import run_hrp_allocation

PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'ADAUSDT', 'XRPUSDT', 'LINKUSDT', 'AVAXUSDT']
OUTPUT_FILE = os.path.join(current_dir, 'hrp_weights.json')
LOG_FILE = os.path.join(current_dir, 'hrp_updater.log')

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + "\n")

def fetch_binance_klines(symbol, limit=30):
    """Télécharge les Klines (bougies 1j) des derniers 30 jours via l'API Binance."""
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1d&limit={limit}"
    for _ in range(3): # Retries
        resp = requests.get(url)
        if resp.status_code == 200:
            data = resp.json()
            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'])
            df['close'] = df['close'].astype(float)
            df.index = pd.to_datetime(df['timestamp'], unit='ms')
            return df['close'].pct_change().rename(symbol.replace('USDT', ''))
        time.sleep(2)
    return None

def update_hrp_weights():
    log("Début de la mise à jour glissante (Rolling Window) HRP...")
    dfs = []
    
    for p in PAIRS:
        series = fetch_binance_klines(p, limit=45) # Prendre un peu plus pour le calcul de variance
        if series is not None:
            dfs.append(series)
        else:
            log(f"Erreur : Impossible de télécharger les données pour {p}")
            return
            
    returns = pd.concat(dfs, axis=1).dropna()
    
    # Exécuter l'algorithme HRP
    log("Calcul de la nouvelle matrice de covariance et des poids HRP...")
    weights, _ = run_hrp_allocation(returns)
    
    # Convertir en dictionnaire
    weights_dict = weights.to_dict()
    
    # Sauvegarder dans le fichier JSON pour le Dashboard et le Pont cTrader
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(weights_dict, f, indent=4)
        
    log("✅ Mise à jour HRP réussie. Nouveaux poids enregistrés.")

if __name__ == "__main__":
    update_hrp_weights()
