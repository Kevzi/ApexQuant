#!/bin/bash
# ApexQuant - Predator V17 - Paper Trading Initialization Script

echo "=========================================================="
echo "🛡️ APEXQUANT PREDATOR V17 - LARK FUNDING PAPER TRADING 🛡️"
echo "=========================================================="

echo "[*] Nettoyage des processus zombies..."
pkill -f freqtrade
pkill -f ctrader_bridge.py
sleep 2

echo "[*] Lancement du cTrader ZMQ Bridge en arrière-plan..."
# Vérifier que le port 5555 est bien bindé
nohup python3 infrastructure/execution/ctrader_bridge.py > /dev/null 2>&1 &
BRIDGE_PID=$!
echo "    [+] Bridge lancé (PID: $BRIDGE_PID)"
sleep 3

echo "[*] Lancement du Master PPO V17 (Unshared Trunks + PFO)..."
# Exécution du dry-run avec la nouvelle configuration v10 isolée
cd ft_userdata && nohup freqtrade trade \
    --strategy FreqaiHybridPPOStrategy \
    --config user_data/config_freqai_rl-v10.json \
    --freqaimodel CustomPPOModel \
    --freqaimodel-path user_data/freqaimodels \
    --dry-run \
    --logfile user_data/logs/freqtrade_paper_v17.log > /dev/null 2>&1 &
FT_PID=$!
cd ..
echo "    [+] Freqtrade lancé (PID: $FT_PID)"

echo "=========================================================="
echo "✅ SYSTÈMES ARMÉS ET OPÉRATIONNELS"
echo "=========================================================="
echo "👉 Pour monitorer la régularisation PFO :"
echo "   tensorboard --logdir ft_userdata/user_data/tensorboard/bybit-futures-predator-v20-unshared-pfo-15m"
echo ""
echo "👉 Pour surveiller les signaux ZMQ en direct :"
echo "   tail -f ft_userdata/user_data/logs/freqtrade_paper_v17.log"
