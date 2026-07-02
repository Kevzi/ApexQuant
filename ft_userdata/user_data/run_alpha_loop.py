import time
import subprocess
import os
import sys

print("Démarrage de la boucle automatique Alpha Pipeline (Toutes les 15 minutes)...")

os.environ["PYTHONIOENCODING"] = "utf-8"

print(f"\n--- Lancement unique du Pipeline par Cron a {time.strftime('%H:%M:%S')} ---")
os.environ["PYTHONIOENCODING"] = "utf-8"

try:
    print("Mise a jour des donnees historiques sur le disque (download-data)...")
    download_cmd = [
        "/root/freqtrade/.env/bin/freqtrade", "download-data",
        "--exchange", "bybit",
        "--trading-mode", "futures",
        "--pairs", "BTC/USDT:USDT", "ETH/USDT:USDT",
        "--timeframes", "15m",
        "--days", "3", # Download only last 3 days to be fast
        "-c", "user_data/config_freqai_rl-v10.json"
    ]
    # Execute from ft_userdata
    ft_dir = os.path.dirname(os.path.abspath(__file__))
    subprocess.run(download_cmd, cwd=os.path.join(ft_dir, ".."), check=False)
    
    print("Calcul des nouveaux signaux via le Pipeline LGBM...")
    script_path = os.path.join(ft_dir, "LGBM_Alpha_Pipeline_V20.py")
    subprocess.run([sys.executable, script_path, "--pairs", "BTC/USDT:USDT,ETH/USDT:USDT"], check=True)
    print("✅ Pipeline execute avec succes. Mise a jour du CSV terminee.")
except Exception as e:
    print(f"❌ Erreur : {e}")
