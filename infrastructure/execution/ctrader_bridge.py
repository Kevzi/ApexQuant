#!/usr/bin/env python3
"""
cTrader Open API ZeroMQ Bridge - Predator V20 - Version 5 (Multi-Account)
Developed for Predator Institutional Architecture V20

This daemon acts as the execution bridge:
1. Listens for ZeroMQ JSON trade signals from Freqtrade on port 5555 using PULL mode.
2. Formats and executes orders on cTrader for multiple accounts simultaneously (Lark Funding + FTMO).
3. Applies Digital Camouflage (time obfuscation, volume jittering, and price offsetting) to evade detection.
4. Decouples Freqtrade from cTrader async reactor loops using a thread-safe worker model.
5. Integrates the TrailingStopManager to handle dynamic, server-side trailing stop-losses per account.
"""

import sys
import os
import json
import time
import random
import threading
import logging
import zmq

# Import the multi-account trailing stop manager v2
try:
    from trailing_stop_manager_v2 import TrailingStopManager
except ImportError:
    # Safe fallback if executed from another folder
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    try:
        from trailing_stop_manager_v2 import TrailingStopManager
    except ImportError:
        TrailingStopManager = None

# Async reactor imports
try:
    from twisted.internet import reactor, defer, task
    from ctrader_open_api import Client, EndPoints, TcpProtocol
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
    from ctrader_open_api.messages.OpenApiMessages_pb2 import *
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
except ImportError as e:
    # Safe mock for offline testing
    Client = object
    logger = logging.getLogger("cTraderBridge")
    logger.warning(f"[!] cTrader Open API dependencies missing: {e}. Using mocks for structural validation.")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [cTrader Bridge V5] - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("ctrader_bridge_v5.log")
    ]
)
logger = logging.getLogger("cTraderBridge")

class CTraderZmqBridgeV5:
    def __init__(self, config_path: str = "ctrader_tokens.json", zmq_port: int = 5555):
        self.config_path = config_path
        self.zmq_port = zmq_port
        self.client = None
        self.access_token = None
        
        # Multi-comptes d'évaluation Lark Funding et FTMO
        self.account_ids = [47737296, 7577274]
        
        self.symbols_map = {}  # account_id -> Symbol Name -> Symbol ID
        self.symbol_id_to_name = {}  # account_id -> Symbol ID -> Symbol Name
        self.is_running = True
        self.balances = {}  # account_id -> balance/equity dict
        
        self.load_configuration()
        
        # Instanciation d'un dictionnaire de gestionnaires TrailingStopManager pour chaque compte
        self.trailing_managers = {}
        if TrailingStopManager is not None:
            for acc_id in self.account_ids:
                self.trailing_managers[acc_id] = TrailingStopManager(self, activation_threshold=0.02, trail_step=0.01, cooldown_seconds=5.0)
            logger.info(f"[Bridge] TrailingStopManagers instanciés avec succès pour les comptes {self.account_ids}.")
        else:
            logger.warning("[Bridge Warning] TrailingStopManager v2 indisponible (ImportError).")

    def load_configuration(self):
        """Loads Spotware ID credentials and Access Token."""
        if not os.path.exists(self.config_path):
            logger.warning(f"Fichier '{self.config_path}' introuvable. Tentative de configuration par défaut.")
            # Creation d'une config mock/par défaut pour éviter de crasher
            default_config = {
                "accessToken": "mock_access_token_v20",
                "client_id": "31781_hUWypKoPcwnQTrOigKcHF20zhj_mock",
                "client_secret": "mock_client_secret_v20",
                "account_ids": self.account_ids
            }
            with open(self.config_path, "w") as f:
                json.dump(default_config, f, indent=4)

        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)
                self.access_token = data.get("accessToken")
                self.client_id = data.get("client_id")
                self.client_secret = data.get("client_secret")
                # Si des comptes spécifiques sont dans le fichier, les fusionner/charger
                loaded_accs = data.get("account_ids", data.get("account_id"))
                if loaded_accs:
                    if isinstance(loaded_accs, list):
                        self.account_ids = [int(x) for x in loaded_accs]
                    else:
                        self.account_ids = [int(loaded_accs)]
        except Exception as e:
            logger.critical(f"Erreur lors du chargement des tokens cTrader : {str(e)}")
            sys.exit(1)

    def start(self):
        """Starts the bridge and initializes both Twisted and ZeroMQ."""
        logger.info(f"Démarrage du Pont d'Exécution Multi-Comptes cTrader (Comptes configurés : {self.account_ids})...")
        
        # Connexion au serveur de test/démo de Spotware (ou live si requis)
        self.client = Client(EndPoints.PROTOBUF_DEMO_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)
        
        # Callback bindings
        self.client.setConnectedCallback(self.on_connected)
        self.client.setMessageReceivedCallback(self.on_message_received)
        self.client.setDisconnectedCallback(self.on_disconnected)
        
        self.heartbeat_loop = task.LoopingCall(self.send_heartbeat)
        self.heartbeat_loop.start(10.0, now=False)
        
        # Start cTrader network connection in Twisted Reactor
        self.client.startService()
        
        # Start ZeroMQ listener in a separate worker thread
        self.zmq_thread = threading.Thread(target=self.zmq_listener_loop, daemon=True)
        self.zmq_thread.start()
        
        # Run Twisted Event Loop
        logger.info("Lancement de Twisted Event Loop...")
        reactor.run(installSignalHandlers=True)

    def send_heartbeat(self):
        try:
            msg = ProtoHeartbeatEvent()
            if self.client:
                d = self.client.send(msg)
                if hasattr(d, 'addErrback'):
                    d.addErrback(lambda f: None)
        except Exception as e:
            pass

    def on_connected(self, client):
        """Called when connected to Spotware OpenAPI server."""
        logger.info("[+] Connexion réseau établie avec connect.spotware.com.")
        
        # Authentifier l'application d'abord (ProtoOAApplicationAuthReq)
        msg = ProtoOAApplicationAuthReq()
        msg.clientId = self.client_id
        msg.clientSecret = self.client_secret
        self.client.send(msg)

    def on_disconnected(self, client, reason):
        logger.warning(f"[-] Déconnecté du serveur cTrader ({reason}). Tentative de reconconnexion asynchrone...")
        time.sleep(5)
        self.client.startService()

    def on_message_received(self, client, message):
        """Message Router from cTrader Open API."""
        try:
            payload_type = message.payloadType
            
            if payload_type == ProtoOAPayloadType.PROTO_OA_APPLICATION_AUTH_RES:
                logger.info("[+] Application de trading autorisée avec succès auprès du serveur Spotware.")
                self.authorize_accounts()
                
            elif payload_type == ProtoOAPayloadType.PROTO_OA_ACCOUNT_AUTH_RES:
                res = ProtoOAAccountAuthRes()
                res.ParseFromString(message.payload)
                acc_id = getattr(res, "ctidTraderAccountId", None)
                if not acc_id:
                    acc_id = message.ctidTraderAccountId if hasattr(message, "ctidTraderAccountId") else self.account_ids[0]
                logger.info(f"[+] Compte de trading {acc_id} autorisé avec succès !")
                self.request_symbols_list(acc_id)
                self.request_account_summary(acc_id)
                
            elif payload_type == ProtoOAPayloadType.PROTO_OA_SYMBOLS_LIST_RES:
                res = ProtoOASymbolsListRes()
                res.ParseFromString(message.payload)
                self.cache_symbols(res)
                
            elif payload_type == ProtoOAPayloadType.PROTO_OA_EXECUTION_EVENT:
                res = ProtoOAExecutionEvent()
                res.ParseFromString(message.payload)
                self.handle_execution_event(res)
                
            elif payload_type == ProtoOAPayloadType.PROTO_OA_SPOT_EVENT:
                res = ProtoOASpotEvent()
                res.ParseFromString(message.payload)
                acc_id = getattr(res, "ctidTraderAccountId", None)
                if acc_id in self.trailing_managers:
                    self.trailing_managers[acc_id].handle_tick(res)
                else:
                    for manager in self.trailing_managers.values():
                        manager.handle_tick(res)
                        
            elif payload_type == ProtoOAPayloadType.PROTO_OA_ASSET_CLASS_LIST_RES:
                pass # Ignoré en production
                
            elif payload_type == ProtoOAPayloadType.PROTO_OA_TRADER_RES:
                res = ProtoOATraderRes()
                res.ParseFromString(message.payload)
                self.handle_account_summary_response(res)
                
            elif payload_type == ProtoOAPayloadType.PROTO_OA_ERROR_RES:
                err = ProtoOAErrorRes()
                err.ParseFromString(message.payload)
                logger.error(f"[-] Erreur OpenAPI reçue : {err.description} (Code: {err.errorCode})")
                
            elif payload_type == ProtoOAPayloadType.PROTO_OA_ORDER_ERROR_EVENT:
                err = ProtoOAOrderErrorEvent()
                err.ParseFromString(message.payload)
                acc_id = getattr(err, "ctidTraderAccountId", None)
                order_id = err.orderId if hasattr(err, "orderId") else "N/A"
                logger.error(f"[-] REJET D'ORDRE cTrader (Compte {acc_id}, Ordre {order_id}) : {err.errorCode} - {err.description}")
                
        except Exception as e:
            logger.error(f"[-] Erreur lors du routage du message OpenAPI : {str(e)}")

    def authorize_accounts(self):
        """Sends account authorization request for each account in ACCOUNT_IDS."""
        for acc_id in self.account_ids:
            logger.info(f"[-] Envoi d'autorisation pour le compte {acc_id}...")
            msg = ProtoOAAccountAuthReq()
            msg.ctidTraderAccountId = int(acc_id)
            msg.accessToken = self.access_token
            self.client.send(msg)

    def request_symbols_list(self, account_id):
        """Requests list of all tradable symbols on specific cTrader account."""
        msg = ProtoOASymbolsListReq()
        msg.ctidTraderAccountId = int(account_id)
        self.client.send(msg)

    def request_account_summary(self, account_id):
        """Demande les informations de balance/equity pour le compte."""
        # Note: Dans OpenAPI, ProtoOATraderReq ou similaire retourne la balance
        try:
            msg = ProtoOATraderReq()
            msg.ctidTraderAccountId = int(account_id)
            self.client.send(msg)
        except Exception as e:
            logger.warning(f"Impossible de demander le résumé du compte {account_id} : {str(e)}")

    def cache_symbols(self, message):
        """Maps Symbol Name -> Symbol ID for rapid trade execution."""
        count = 0
        acc_id = str(getattr(message, "ctidTraderAccountId", "Global"))
        if acc_id not in self.symbols_map:
            self.symbols_map[acc_id] = {}
            self.symbol_id_to_name[acc_id] = {}
            
        for s in message.symbol:
            name_upper = s.symbolName.upper()
            self.symbols_map[acc_id][name_upper] = s.symbolId
            self.symbol_id_to_name[acc_id][s.symbolId] = name_upper
            count += 1
        logger.info(f"[+] {count} symboles synchronisés pour le compte {acc_id} (ex: BTCUSD, ETHUSD).")

    def handle_account_summary_response(self, message):
        """Met à jour lark_balance.json de manière globale avec les deux comptes pour le Dashboard."""
        try:
            acc_id = message.ctidTraderAccountId
            trader = message.trader
            balance = trader.balance / 100.0  # Division par 100 si Spotware stocke en centimes
            
            # Stockage
            self.balances[str(acc_id)] = {
                "balance": balance,
                "equity": getattr(trader, "equity", balance * 100) / 100.0,
                "timestamp": time.time()
            }
            
            # Écriture asynchrone du dictionnaire global
            with open("lark_balance.json", "w") as f:
                json.dump(self.balances, f, indent=4)
                
            logger.info(f"[Balances] lark_balance.json mis à jour pour le compte {acc_id} (Balance: {balance:.2f}$).")
        except Exception as e:
            logger.error(f"[-] Erreur lors du traitement du résumé de compte : {str(e)}")

    # -------------------------------------------------------------------------
    # GESTION DES SIGNAUX ZEROMQ (MULTIPLE-ACCOUNTS)
    # -------------------------------------------------------------------------
    def zmq_listener_loop(self):
        """ZeroMQ PULL listener thread (Haute Fiabilité)."""
        context = zmq.Context()
        socket = context.socket(zmq.PULL)
        bind_address = f"tcp://127.0.0.1:{self.zmq_port}"
        logger.info(f"[-] Démarrage du récepteur ZeroMQ PULL sur {bind_address}...")
        socket.bind(bind_address)

        while self.is_running:
            try:
                message_str = socket.recv_string(flags=zmq.NOBLOCK)
                logger.info(f"[ZMQ PULL] Signal reçu de Freqtrade : {message_str}")
                signal = json.loads(message_str)
                
                # Routage Twisted de manière thread-safe
                reactor.callFromThread(self.process_zmq_signal, signal)
            except zmq.Again:
                time.sleep(0.01)
            except Exception as e:
                logger.error(f"[ZMQ PULL] Erreur du récepteur : {str(e)}")
                time.sleep(1)

    def process_zmq_signal(self, signal: dict):
        """Exécuté de manière thread-safe dans la boucle Twisted."""
        pair = signal.get("pair")
        action = signal.get("action", "").upper()
        direction = signal.get("direction", "").upper()
        amount = float(signal.get("amount", 0.0))
        price = float(signal.get("price", 0.0))
        sl_percentage = float(signal.get("sl_percentage", 0.0))
        target_account = signal.get("target_account")  # Facultatif : cibler un seul compte
        
        # Mapping logique croisée (ENTRY/EXIT + LONG/SHORT) vers le Side cTrader
        if action == "ENTRY" and direction == "LONG":
            side_str = "BUY"
        elif action == "ENTRY" and direction == "SHORT":
            side_str = "SELL"
        elif action == "EXIT" and direction == "LONG":
            side_str = "SELL"
        elif action == "EXIT" and direction == "SHORT":
            side_str = "BUY"
        else:
            side_str = "UNKNOWN"

        if not pair:
            logger.error("[Bridge ZMQ] Message reçu sans paire d'actif.")
            return

        normalized_symbol = self.normalize_pair_name(pair)
        
        # Boucle d'envoi et de duplication asynchrone pour chaque compte d'évaluation
        for acc_id in self.account_ids:
            if target_account and str(acc_id) != str(target_account):
                continue
                
            acc_str = str(acc_id)
            if acc_str not in self.symbols_map:
                logger.error(f"[Bridge] Symboles non chargés pour le compte {acc_id}.")
                continue
                
            symbol_id = self.symbols_map[acc_str].get(normalized_symbol)
            if not symbol_id:
                fallback = normalized_symbol.replace("/", "").replace("_", "")
                symbol_id = self.symbols_map[acc_str].get(fallback)
                if symbol_id:
                    normalized_symbol = fallback
            
            # 1. OBSTRUCTION TEMPORELLE (Execution Delay de 1.0 à 5.0 secondes par compte)
            delay = random.uniform(1.0, 5.0)
            logger.info(f"[Camouflage] Planification de l'ordre sur le compte {acc_id} dans {delay:.2f} secondes pour casser la corrélation.")
            reactor.callLater(delay, self.execute_order_for_account, acc_id, normalized_symbol, symbol_id, side_str, amount, price, sl_percentage)

    def execute_order_for_account(self, account_id: int, symbol: str, symbol_id: int, side: str, amount: float, price: float = 0.0, sl_percentage: float = 0.0):
        """Envoie l'ordre de marché sur un compte spécifique avec Jittering de Volume."""
        if not symbol_id:
            logger.error(f"[Bridge Execution] Impossible de mapper {symbol} pour le compte {account_id}.")
            return
            
        # 2. VARIABILITÉ DE VOLUME (Jittering de volume entre 1.0% et 2.0% avec arrondi conforme au step-size)
        raw_volume = amount * 100
        jitter = random.uniform(0.98, 1.02)
        jittered_volume = raw_volume * jitter
        
        # Arrondi strict au step size le plus proche (1 volume unitaire cTrader OpenAPI = 0.01 lot)
        volume = int(round(jittered_volume))
        # --- FIX: PROP FIRM MINIMUM VOLUME ENFORCEMENT ---
        # Si le volume calculé est trop faible pour les Prop Firms strictes (ex: min 0.10 lot pour crypto)
        # On sécurise l'exécution en arrondissant au minimum requis (10 unités = 0.10 lot)
        if "BTC" in symbol or "ETH" in symbol:
            volume = max(10, volume)
        else:
            volume = max(1, volume) # Protection anti-zero par défaut
        
        logger.info(f"[Bridge Execution] [Compte: {account_id}] Envoi de l'ordre : {side} | {amount} lots originaux -> Jittered {volume/100:.2f} lots sur {symbol} (ID: {symbol_id})")
        
        msg = ProtoOANewOrderReq()
        msg.ctidTraderAccountId = int(account_id)
        msg.symbolId = symbol_id
        msg.volume = volume
        
        if side in ("BUY", "ENTER_LONG", "EXIT_SHORT"):
            msg.tradeSide = ProtoOATradeSide.BUY
        elif side in ("SELL", "ENTER_SHORT", "EXIT_LONG"):
            msg.tradeSide = ProtoOATradeSide.SELL
        else:
            logger.error(f"[-] Side inconnu : {side} pour le compte {account_id}. Ordre avorté.")
            return
            
        if price > 0.0:
            msg.orderType = ProtoOAOrderType.LIMIT
            if msg.tradeSide == ProtoOATradeSide.BUY:
                msg.limitPrice = float(round(price * 1.02, 2))
            else:
                msg.limitPrice = float(round(price * 0.98, 2))
        else:
            msg.orderType = ProtoOAOrderType.MARKET
        
        msg.timeInForce = ProtoOATimeInForce.IMMEDIATE_OR_CANCEL
        
        # --- FIX: HARD STOP-LOSS FOR STRICT PROP FIRMS ---
        # Lark Funding et d'autres Prop Firms ferment instantanément le trade si aucun SL n'est attaché.
        if price > 0 and sl_percentage != 0:
            if msg.tradeSide == ProtoOATradeSide.BUY:
                stop_loss_price = price * (1 + sl_percentage) # sl_percentage est négatif
            else:
                stop_loss_price = price * (1 - sl_percentage)
                
            msg.stopLoss = float(round(stop_loss_price, 2))
            logger.info(f"[Bridge Execution] Hard Stop-Loss attaché: {stop_loss_price:.5f} (Prop Firm Compliance)")
        
        if self.client:
            self.client.send(msg)
            logger.info(f"[Bridge Execution] [Compte: {account_id}] Requête ProtoOANewOrderReq transmise (Vol: {msg.volume}).")

    def handle_execution_event(self, message):
        """Intercepté lors des événements d'exécution Spotware pour chaque compte."""
        try:
            exec_type = message.executionType
            order_id = message.order.orderId if message.HasField("order") else "N/A"
            acc_id = getattr(message, "ctidTraderAccountId", None)
            
            logger.info(f"[+] EVENT : Ordre cTrader traité ! ID: {order_id} | Type: {exec_type} | Compte: {acc_id}")
            
            # Routage vers le TrailingStopManager correspondant à l'Account ID
            if acc_id in self.trailing_managers:
                manager = self.trailing_managers[acc_id]
                
                is_filled = False
                if exec_type is not None:
                    if str(exec_type) in ("ORDER_FILLED", "3") or exec_type == 3 or getattr(exec_type, "name", "") == "ORDER_FILLED":
                        is_filled = True
                
                if is_filled:
                    manager.register_position(message)
                
                # Si la position a été clôturée
                if hasattr(message, "position") and message.position:
                    position_status = getattr(message.position, "positionStatus", None)
                    is_closed = False
                    if position_status is not None:
                        if str(position_status) == "PROTO_OA_POSITION_STATUS_CLOSED" or position_status == 2 or getattr(position_status, "name", "") == "PROTO_OA_POSITION_STATUS_CLOSED":
                            is_closed = True
                    
                    if is_closed or getattr(message.position, "volume", 1) == 0:
                        manager.remove_position(message.position.positionId, message.ctidTraderAccountId)
                
        except Exception as e:
            logger.error(f"[-] Erreur lors de l'interception de l'événement d'exécution : {str(e)}")

    def normalize_pair_name(self, pair: str) -> str:
        """Standardise la paire de devises et convertit USDT en USD pour cTrader (FTMO/Lark)."""
        # Ex: pair = "BTC/USDT:USDT"
        base = pair.split("/")[0].upper()
        quote = pair.split("/")[1].split(":")[0].upper()
        if quote == "USDT":
            quote = "USD"
        return f"{base}{quote}"

    def stop(self):
        self.is_running = False
        if reactor.running:
            reactor.stop()

if __name__ == "__main__":
    bridge = CTraderZmqBridgeV5()
    try:
        bridge.start()
    except KeyboardInterrupt:
        logger.info("Fermeture propre du Pont cTrader...")
        bridge.stop()
