"""
Télécharge l'historique OI + funding + order flow + liquidations depuis Velo Data
pour BTC et ETH (perp Bybit), depuis 2021, en 1h, et sauvegarde en CSV.

Usage :
  1. Mets ta clé API Velo dans API_KEY ci-dessous.
  2. /home/kevin/freqtrade/.env/bin/python3 user_data/download_velo.py
"""
from velodata import lib as velo
import pandas as pd

API_KEY = "COLLE_TA_CLE_ICI"   # <-- ta clé API Velo

OUT_DIR = "/mnt/c/Users/kevin/Downloads/Projet code/ApexQuantV2/ft_userdata/user_data"
EXCHANGE = "bybit"             # même venue que tes données prix
COLUMNS = ['close_price', 'coin_open_interest_close', 'funding_rate',
           'buy_coin_volume', 'sell_coin_volume',
           'buy_liquidations', 'sell_liquidations']

client = velo.client(API_KEY)

for coin, product in [('BTC', 'BTCUSDT'), ('ETH', 'ETHUSDT')]:
    print(f"\n=== Téléchargement {coin} ({product}) ===")
    params = {
        'type': 'futures',
        'columns': COLUMNS,
        'exchanges': [EXCHANGE],
        'products': [product],
        'begin': 1609459200000,          # 2021-01-01
        'end': client.timestamp(),
        'resolution': '1h'
    }
    # batch_rows découpe automatiquement les grosses requêtes
    frames = []
    try:
        batches = client.batch_rows(params)
        for df in client.stream_rows(batches):
            frames.append(df)
            print(f"  ... {sum(len(f) for f in frames)} lignes", end='\r')
    except Exception as e:
        print("Batch a échoué, tentative get_rows simple:", repr(e)[:150])
        frames = [client.get_rows(params)]
    full = pd.concat(frames, ignore_index=True)
    out = f"{OUT_DIR}/velo_{coin}_1h.csv"
    full.to_csv(out, index=False)
    print(f"\n  [OK] {len(full)} lignes -> {out}")
    print(full.tail(3).to_string())

print("\n=== TERMINÉ. Dis à Claude que les CSV sont prêts. ===")
