#!/bin/bash
# ApexQuant - Predator V17 - LIVE TRADING SCRIPT (Lark Funding)

echo "=========================================================="
echo "🛡️ APEXQUANT PREDATOR V17 - LIVE TRADING (LARK FUNDING) 🛡️"
echo "=========================================================="

echo "[*] Nettoyage des processus zombies..."
pkill -9 -f freqtrade
pkill -9 -f ctrader_bridge.py
pkill -9 -f streamlit
sleep 3


echo "[*] Lancement du cTrader ZMQ Bridge en arrière-plan..."
source ~/freqtrade/.env/bin/activate
nohup python infrastructure/execution/ctrader_bridge.py > infrastructure/execution/bridge_live.log 2>&1 &
BRIDGE_PID=$!
echo "    [+] Bridge cTrader lancé (PID: $BRIDGE_PID)"
sleep 3

echo "[*] Lancement du Master PPO V17 en mode LIVE..."
cd ft_userdata
nohup freqtrade trade \
    -c user_data/config_live.json \
    -c user_data/config_freqai_rl-v10.json \
    --strategy FreqaiHybridPPOStrategy \
    --freqaimodel CustomPPOModel \
    --logfile user_data/logs/freqtrade_live_v17.log > ../infrastructure/execution/freqtrade_startup.log 2>&1 &
FT_PID=$!
cd ..
echo "    [+] Freqtrade AI Live lancé (PID: $FT_PID)"
sleep 3

echo "[*] Lancement du Dashboard ApexQuant Central..."
nohup streamlit run infrastructure/monitoring/apexquant_dashboard.py --server.port 8501 --server.headless true > /dev/null 2>&1 &
DASH_PID=$!
echo "    [+] Dashboard Streamlit lancé (PID: $DASH_PID)"

echo "=========================================================="
echo "✅ SYSTÈMES ARMÉS ET OPÉRATIONNELS"
echo "=========================================================="
echo "👉 Dashboard de monitoring LIVE accessible sur : http://localhost:8501"
echo "👉 Script Heartbeat exécutable manuellement : python infrastructure/execution/heartbeat_trade.py"
