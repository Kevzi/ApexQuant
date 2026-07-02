#!/usr/bin/env python3
"""
Stop-Loss Server-Side Synchronizer & Trailing Stop - Predator V20 - Version 3
Héberge le Stop-Loss directement côté serveur chez Lark Funding + FTMO
et implémente un algorithme de Stop Suiveur (Trailing Stop) dynamique avec Price Offsetting (Camouflage).
"""

import time
import logging
import random

# Configure logging
logger = logging.getLogger("StopLossSynchronizer")

try:
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
    from ctrader_open_api.messages.OpenApiMessages_pb2 import *
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
except ImportError:
    # Mocks pour la validation de structure hors-ligne
    ProtoOAAmendPositionSLTPReq = object
    logger.warning("[!] openapi protobuf messages unavailable. Using mocks for structural validation.")

class StopLossSynchronizerV3:
    def __init__(self, bridge):
        self.bridge = bridge
        # cache key: symbol_name (e.g., 'BTCUSD') or symbol_id (int) -> value: float (ex: 0.05 pour 5%)
        self.sl_percentage_cache = {}  
        # Dictionnaire des positions actives à suivre pour le Trailing Stop
        # ID -> {symbol_id, symbol_name, is_buy, entry_price, highest_price, lowest_price, current_sl, sl_percentage, ctid_account_id}
        self.active_positions = {}

    def store_sl_percentage(self, key, sl_percentage: float):
        """Enregistre le pourcentage de stop-loss pour un actif donné."""
        self.sl_percentage_cache[key] = float(sl_percentage)
        logger.info(f"[SL Sync Cache] Stop Loss % enregistré pour {key} : {sl_percentage:.4f}")

    def apply_sl_to_position(self, message):
        """
        Déclenché lors de la réception d'un événement d'exécution ORDER_FILLED.
        Calcule, applique et enregistre la position pour le suivi Trailing Stop.
        """
        try:
            exec_type = getattr(message, "executionType", None)
            ctid_account_id = getattr(message, "ctidTraderAccountId", None)
            
            # Extraction et nettoyage des objets de position
            if not hasattr(message, "position") or not message.position:
                return None
                
            position = message.position
            position_id = position.positionId
            symbol_id = position.symbolId
            trade_side = position.tradeSide # BUY = 1, SELL = 2
            
            # 1. Vérification de la fermeture de la position
            position_status = getattr(position, "positionStatus", None)
            is_closed = False
            if position_status is not None:
                if str(position_status) == "PROTO_OA_POSITION_STATUS_CLOSED" or position_status == 2 or getattr(position_status, "name", "") == "PROTO_OA_POSITION_STATUS_CLOSED":
                    is_closed = True
            
            if is_closed or getattr(position, "volume", 1) == 0:
                if position_id in self.active_positions:
                    logger.info(f"[SL Sync] [Compte: {ctid_account_id}] La position {position_id} est fermée. Retrait du suivi Trailing Stop.")
                    self.active_positions.pop(position_id, None)
                return None

            # 2. Traitement du ORDER_FILLED pour l'initialisation du Stop-Loss
            is_filled = False
            if exec_type is not None:
                if str(exec_type) == "ORDER_FILLED" or exec_type == 1 or getattr(exec_type, "name", "") == "ORDER_FILLED":
                    is_filled = True
            
            if not is_filled:
                return None
                
            # Extraction du prix d'exécution réel (ordre ou position)
            execution_price = None
            if hasattr(message, "order") and message.order and hasattr(message.order, "executionPrice"):
                execution_price = message.order.executionPrice
            
            if not execution_price:
                execution_price = getattr(position, "entryPrice", None)
                
            if not execution_price:
                logger.error(f"[SL Sync] [Compte: {ctid_account_id}] Impossible de trouver le prix d'exécution ou d'entrée sur l'événement.")
                return None

            # Récupération du pourcentage de SL (par ID ou nom de symbole)
            sl_percentage = self.sl_percentage_cache.get(symbol_id)
            symbol_name = "Unknown"
            if sl_percentage is None:
                if hasattr(self.bridge, "symbol_id_to_name"):
                    symbol_name = self.bridge.symbol_id_to_name.get(symbol_id, "Unknown")
                    sl_percentage = self.sl_percentage_cache.get(symbol_name)
            else:
                if hasattr(self.bridge, "symbol_id_to_name"):
                    symbol_name = self.bridge.symbol_id_to_name.get(symbol_id, "Unknown")

            if sl_percentage is None:
                logger.info(f"[SL Sync] [Compte: {ctid_account_id}] Aucun pourcentage de SL en cache pour le Symbole ID {symbol_id} ({symbol_name}). Synchronisation ignorée.")
                return None

            # Calcul du prix de stop-loss absolu initial
            abs_sl = abs(sl_percentage)
            
            # BUY = 1, SELL = 2
            is_buy = False
            if trade_side == 1 or str(trade_side) == "BUY" or getattr(trade_side, "name", "") == "BUY":
                is_buy = True
                
            if is_buy:
                # Pour un LONG, le SL est en-dessous de l'entrée
                sl_price = execution_price * (1.0 - abs_sl)
            else:
                # Pour un SHORT, le SL est au-dessus de l'entrée
                sl_price = execution_price * (1.0 + abs_sl)

            logger.info(f"[SL Sync] [Compte: {ctid_account_id}] Initialisation d'un ordre validé pour {symbol_name} (ID: {symbol_id}, Position: {position_id}) au prix de {execution_price:.5f}")
            logger.info(f"[SL Sync] [Compte: {ctid_account_id}] Alignement du Stop Loss initial serveur -> Prix Cible : {sl_price:.5f} (SL %: {sl_percentage:.4f})")

            # 3. Enregistrement de la position active pour le suivi du Trailing Stop
            self.active_positions[position_id] = {
                "symbol_id": symbol_id,
                "symbol_name": symbol_name,
                "is_buy": is_buy,
                "entry_price": float(execution_price),
                "highest_price": float(execution_price),
                "lowest_price": float(execution_price),
                "current_sl": float(sl_price),
                "sl_percentage": float(sl_percentage),
                "ctid_account_id": ctid_account_id
            }
            logger.info(f"[SL Sync] [Compte: {ctid_account_id}] Position {position_id} ajoutée au tracker de Trailing Stop.")

            # Construction de la requête ProtoOAAmendPositionSLTPReq
            return self.send_amend_sl_request(ctid_account_id, position_id, sl_price)

        except Exception as e:
            logger.error(f"[SL Sync] Erreur critique lors de la synchronisation du Stop-Loss : {str(e)}")
            return None

    def handle_spot_event(self, message):
        """
        Déclenché lors de la réception d'un événement PROTO_OA_SPOT_EVENT.
        Met à jour le cours, vérifie les sommets/creux et fait glisser le Stop-Loss en mode Trailing.
        """
        try:
            symbol_id = message.symbolId
            bid = getattr(message, "bid", None)
            ask = getattr(message, "ask", None)
            ctid_account_id = getattr(message, "ctidTraderAccountId", None)
            
            if not bid and not ask:
                return

            # Parcours des positions actives suivies
            for pos_id, pos in list(self.active_positions.items()):
                if pos["symbol_id"] != symbol_id:
                    continue
                if ctid_account_id is not None and pos["ctid_account_id"] != ctid_account_id:
                    continue

                is_buy = pos["is_buy"]
                current_price = float(bid if is_buy else ask)
                if not current_price:
                    current_price = float(bid or ask)

                abs_sl = abs(pos["sl_percentage"])

                if is_buy:  # Position LONG
                    # Si le prix actuel fait un nouveau sommet
                    if current_price > pos["highest_price"]:
                        pos["highest_price"] = current_price
                        new_sl_price = current_price * (1.0 - abs_sl)

                        # Le SL ne peut que monter
                        if new_sl_price > pos["current_sl"]:
                            logger.info(f"[Trailing Stop] [Compte: {pos['ctid_account_id']}] Nouveau sommet détecté pour {pos['symbol_name']} (Pos: {pos_id}) : {current_price:.5f}")
                            logger.info(f"[Trailing Stop] [Compte: {pos['ctid_account_id']}] Glissement du Stop Loss vers le haut : {pos['current_sl']:.5f} -> {new_sl_price:.5f}")
                            pos["current_sl"] = new_sl_price
                            self.send_amend_sl_request(pos['ctid_account_id'], pos_id, new_sl_price)

                else:  # Position SHORT
                    # Si le prix actuel fait un nouveau creux
                    if current_price < pos["lowest_price"]:
                        pos["lowest_price"] = current_price
                        new_sl_price = current_price * (1.0 + abs_sl)

                        # Le SL ne peut que descendre
                        if new_sl_price < pos["current_sl"]:
                            logger.info(f"[Trailing Stop] [Compte: {pos['ctid_account_id']}] Nouveau creux détecté pour {pos['symbol_name']} (Pos: {pos_id}) : {current_price:.5f}")
                            logger.info(f"[Trailing Stop] [Compte: {pos['ctid_account_id']}] Glissement du Stop Loss vers le bas : {pos['current_sl']:.5f} -> {new_sl_price:.5f}")
                            pos["current_sl"] = new_sl_price
                            self.send_amend_sl_request(pos['ctid_account_id'], pos_id, new_sl_price)

        except Exception as e:
            logger.error(f"[Trailing Stop Error] Erreur de traitement du Spot Event : {str(e)}")

    def send_amend_sl_request(self, ctid_trader_account_id, position_id, sl_price: float):
        """Formate et envoie la requête ProtoOAAmendPositionSLTPReq au serveur cTrader avec Price Offsetting."""
        try:
            try:
                from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAAmendPositionSLTPReq
            except ImportError:
                ProtoOAAmendPositionSLTPReq = None

            if ProtoOAAmendPositionSLTPReq is None:
                logger.warning(f"[SL Sync] [Compte: {ctid_trader_account_id}] Requête de modification simulée avec succès (OpenAPI non chargé).")
                return None

            # Price Offsetting (bruit stochastique de décalage de prix de 0.01% à 0.05% pour éviter les signatures identiques)
            offset_percentage = random.uniform(0.0001, 0.0005)
            offset_sign = 1 if (int(ctid_trader_account_id) % 2 == 0) else -1
            sl_price_offset = sl_price * (1.0 + (offset_sign * offset_percentage))

            msg = ProtoOAAmendPositionSLTPReq()
            msg.ctidTraderAccountId = int(ctid_trader_account_id)
            msg.positionId = int(position_id)
            msg.stopLoss = round(float(sl_price_offset), 5)

            if self.bridge and self.bridge.client:
                self.bridge.client.send(msg)
                logger.info(f"[SL Sync] [Compte: {ctid_trader_account_id}] [Camouflage Price Offset: {offset_sign * offset_percentage * 100:.4f}%] Requête de modification envoyée pour la position {position_id} (SL Cible original: {sl_price:.5f}, SL bruité: {msg.stopLoss:.5f})")
                return msg
            else:
                logger.error(f"[SL Sync] [Compte: {ctid_trader_account_id}] Pont ou client cTrader non connecté.")
                return None
        except Exception as e:
            logger.error(f"[SL Sync Error] Impossible d'envoyer la modification de SL : {str(e)}")
            return None
