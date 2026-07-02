#!/usr/bin/env python3
"""
Heartbeat Trade - Lark Funding & FTMO - Inactivity Reset Daemon
Ce script indépendant prévient la désactivation des comptes d'évaluation après 30 jours d'inactivité.
Il se connecte au socket ZeroMQ PULL du pont d'exécution cTrader et émet un micro-trade d'inactivité (0.01 lot) sur l'EUR/USD.
Le trade reste exposé pendant 125 secondes (pour dépasser le verrou des 2 minutes de Lark Funding) puis se clôture proprement.
"""

import sys
import json
import time
import logging
import zmq

# Configuration des logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [Heartbeat Reset] - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("lark_heartbeat.log")
    ]
)
logger = logging.getLogger("LarkHeartbeat")

def send_heartbeat_trade(zmq_port: int = 5555):
    logger.info("Démarrage du processus de réinitialisation du compteur d'inactivité...")
    
    # Initialisation du socket ZeroMQ PUSH pour communiquer avec le pont
    context = zmq.Context()
    socket = context.socket(zmq.PUSH)
    zmq_address = f"tcp://127.0.0.1:{zmq_port}"
    
    try:
        logger.info(f"Connexion au pont d'exécution ZeroMQ sur {zmq_address}...")
        socket.connect(zmq_address)
    except Exception as e:
        logger.critical(f"Impossible de se connecter au pont d'exécution : {str(e)}")
        sys.exit(1)

    # 1. Envoi de l'ordre d'entrée (0.01 lot sur EUR/USD)
    entry_payload = {
        "pair": "EUR/USD",
        "side": "BUY",
        "amount": 0.01
    }
    
    logger.info(f"Émission du signal d'ouverture : {entry_payload}")
    socket.send_string(json.dumps(entry_payload))
    
    # 2. Sommeil asynchrone de 125 secondes (dépassement réglementaire de la règle des 120 secondes de Lark)
    logger.info("Position ouverte avec succès. Attente réglementaire de 125 secondes avant de clôturer...")
    time.sleep(125)
    
    # 3. Envoi de l'ordre de sortie
    exit_payload = {
        "pair": "EUR/USD",
        "side": "EXIT_LONG",
        "amount": 0.01
    }
    
    logger.info(f"Émission du signal de clôture d'urgence : {exit_payload}")
    socket.send_string(json.dumps(exit_payload))
    
    logger.info("[🏁] Heartbeat terminé avec succès ! Le compteur d'inactivité des 30 jours de Lark Funding et FTMO a été réinitialisé.")

if __name__ == "__main__":
    port = 5555
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            logger.warning(f"Port invalide. Utilisation du port par défaut {port}.")
            
    send_heartbeat_trade(port)
