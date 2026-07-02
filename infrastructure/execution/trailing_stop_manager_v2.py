#!/usr/bin/env python3
"""
Trailing Stop Manager - Predator V20 - Version 2 (Production Grade Multi-Account)
Gère l'activation et le glissement dynamique des Stop-Loss côté serveur sur cTrader.
Intègre un module de Throttling et de Price Offsetting pour le camouflage multi-comptes (Lark Funding + FTMO).
"""

import time
import math
import logging
import random

logger = logging.getLogger("TrailingStopManager")

try:
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
    from ctrader_open_api.messages.OpenApiMessages_pb2 import *
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
except ImportError:
    # Mocks pour la validation de structure hors-ligne
    ProtoOASubscribeSpotsReq = object
    ProtoOAUnsubscribeSpotsReq = object
    ProtoOAAmendPositionSLTPReq = object
    logger.warning("[!] openapi protobuf messages unavailable. Using mocks for structural validation.")

class TrailingStopManager:
    def __init__(self, bridge, activation_threshold: float = 0.02, trail_step: float = 0.01, cooldown_seconds: float = 5.0):
        """
        :param bridge: Le pont d'exécution cTrader principal.
        :param activation_threshold: Seuil de profit latent pour activer le Trailing (ex: 0.02 pour 2.0%).
        :param trail_step: Distance de retrait du SL derrière le prix (ex: 0.01 pour 1.0%).
        :param cooldown_seconds: Temps d'attente minimal entre deux requêtes de modification d'un même trade (anti-spam).
        """
        self.bridge = bridge
        self.activation_threshold = float(activation_threshold)
        self.trail_step = float(trail_step)
        self.cooldown_seconds = float(cooldown_seconds)
        
        # Structure de suivi des positions actives :
        # position_id (int) -> dict
        self.tracked_positions = {}

    def register_position(self, message):
        """
        Enregistre une nouvelle position ouverte suite à un événement ORDER_FILLED.
        """
        try:
            if not hasattr(message, "position") or not message.position:
                return

            position = message.position
            position_id = position.positionId
            
            # Ignorer les positions clôturées
            position_status = getattr(position, "positionStatus", None)
            if position_status is not None:
                if str(position_status) == "PROTO_OA_POSITION_STATUS_CLOSED" or position_status == 2 or getattr(position_status, "name", "") == "PROTO_OA_POSITION_STATUS_CLOSED":
                    logger.info(f"[Trailing Manager] Ignoré : la position {position_id} est déjà clôturée.")
                    return

            trade_data = getattr(position, "tradeData", position)
            symbol_id = getattr(position, "symbolId", getattr(trade_data, "symbolId", None))
            trade_side = getattr(position, "tradeSide", getattr(trade_data, "tradeSide", None)) # BUY = 1, SELL = 2
            ctid_account_id = getattr(message, "ctidTraderAccountId", "Unknown")
            
            # Extraction du prix d'entrée réel
            execution_price = None
            if hasattr(message, "order") and message.order and hasattr(message.order, "executionPrice"):
                execution_price = message.order.executionPrice
            
            if not execution_price:
                execution_price = getattr(position, "entryPrice", None)
                
            if not execution_price:
                logger.error(f"[Trailing Manager] Impossible de trouver le prix d'entrée pour la position {position_id} du compte {ctid_account_id}.")
                return

            symbol_name = "Unknown"
            if hasattr(self.bridge, "symbol_id_to_name"):
                symbol_name = self.bridge.symbol_id_to_name.get(symbol_id, "Unknown")

            is_buy = (trade_side == 1 or str(trade_side) == "BUY" or getattr(trade_side, "name", "") == "BUY")

            # Enregistrement initial
            self.tracked_positions[position_id] = {
                "symbol_id": symbol_id,
                "symbol_name": symbol_name,
                "is_buy": is_buy,
                "entry_price": float(execution_price),
                "highest_price": float(execution_price),
                "lowest_price": float(execution_price),
                "current_sl": None,  # Défini lors du premier amendement
                "is_trailing_active": False,  # S'active une fois le threshold franchi
                "last_update_time": 0.0,  # Pour le throttling anti-spam
                "ctid_account_id": ctid_account_id
            }
            
            logger.info(f"[Trailing Manager] [Compte: {ctid_account_id}] Position {position_id} ({symbol_name}) enregistrée à l'entrée {execution_price:.5f} (Trigger de Trailing à {self.activation_threshold*100}%).")
            
            # Émission asynchrone d'une requête d'abonnement aux cotations temps réel (Spots)
            self.subscribe_to_spots(ctid_account_id, symbol_id)

        except Exception as e:
            logger.error(f"[Trailing Manager Error] Échec de l'enregistrement de la position : {str(e)}")

    def remove_position(self, position_id: int, ctid_trader_account_id: int):
        """
        Retire une position fermée du suivi et se désabonne du flux si aucune autre position ne l'utilise.
        """
        if position_id in self.tracked_positions:
            pos = self.tracked_positions.pop(position_id)
            symbol_id = pos["symbol_id"]
            symbol_name = pos["symbol_name"]
            logger.info(f"[Trailing Manager] [Compte: {ctid_trader_account_id}] Position {position_id} ({symbol_name}) retirée du suivi dynamique.")
            
            # Vérification s'il reste d'autres positions actives sur le même symbole
            still_active = any(p["symbol_id"] == symbol_id for p in self.tracked_positions.values())
            if not still_active:
                logger.info(f"[Trailing Manager] [Compte: {ctid_trader_account_id}] Plus de positions ouvertes pour {symbol_name}. Désabonnement du flux temps réel.")
                self.unsubscribe_from_spots(ctid_trader_account_id, symbol_id)

    def handle_tick(self, message):
        """
        Traite un événement de prix temps réel (ProtoOASpotEvent).
        Calcule les ratios de profit flottant, active le Trailing, et ajuste le SL avec Throttling.
        """
        try:
            symbol_id = message.symbolId
            bid = getattr(message, "bid", None)
            ask = getattr(message, "ask", None)
            ctid_account_id = getattr(message, "ctidTraderAccountId", None)
            
            if not bid and not ask:
                return

            now = time.time()

            # Itération sur une copie des clés pour éviter les erreurs de mutation concurrente
            for pos_id, pos in list(self.tracked_positions.items()):
                if pos["symbol_id"] != symbol_id:
                    continue
                # S'assurer que l'événement correspond bien au compte de la position
                if ctid_account_id is not None and pos["ctid_account_id"] != ctid_account_id:
                    continue

                is_buy = pos["is_buy"]
                raw_price = float(bid if is_buy else ask)
                if not raw_price:
                    raw_price = float(bid or ask)

                entry_price = pos["entry_price"]
                
                # Auto-découverte du diviseur Spotware (10^x) pour corriger les prix bruts (ex: 155900000 -> 1559.00)
                if entry_price > 0 and raw_price > 0:
                    power = round(math.log10(raw_price / entry_price))
                    current_price = raw_price / (10 ** power)
                else:
                    current_price = raw_price
                
                # 1. Calcul du profit flottant (PnL)
                if is_buy:
                    floating_pnl = (current_price - entry_price) / entry_price
                else:
                    floating_pnl = (entry_price - current_price) / entry_price

                # 2. Activation du mécanisme de Trailing Stop
                if not pos["is_trailing_active"]:
                    if floating_pnl >= self.activation_threshold:
                        pos["is_trailing_active"] = True
                        logger.info(f"[Trailing Activated] [Compte: {pos['ctid_account_id']}] 🚀 Seuil franchi pour {pos['symbol_name']} (Pos: {pos_id}) : PnL Flottant à {floating_pnl*100:.2f}% (Seuil: {self.activation_threshold*100}%). Activation du verrou serveur.")
                    else:
                        continue # Trailing inactif, on attend que le prix monte au seuil requis

                # 3. Traque des performances extrêmes (High/Low Watermark)
                if is_buy:
                    if current_price > pos["highest_price"]:
                        pos["highest_price"] = current_price
                        target_sl_price = current_price * (1.0 - self.trail_step)
                        
                        # Vérification de la pertinence du déplacement (Le SL ne peut que monter)
                        if pos["current_sl"] is None or target_sl_price > pos["current_sl"]:
                            # Throttling anti-spam (cooldown)
                            if now - pos["last_update_time"] >= self.cooldown_seconds:
                                pos["current_sl"] = target_sl_price
                                pos["last_update_time"] = now
                                logger.info(f"[Trailing Long] [Compte: {pos['ctid_account_id']}] Déplacement du SL (Pos: {pos_id}) : {target_sl_price:.5f} (Distance: {self.trail_step*100}% derrière le haut à {current_price:.5f})")
                                self.send_amend_sl_request(pos['ctid_account_id'], pos_id, target_sl_price)
                else:
                    if current_price < pos["lowest_price"]:
                        pos["lowest_price"] = current_price
                        target_sl_price = current_price * (1.0 + self.trail_step)
                        
                        # Vérification de la pertinence du déplacement (Le SL ne peut que descendre)
                        if pos["current_sl"] is None or target_sl_price < pos["current_sl"]:
                            # Throttling anti-spam (cooldown)
                            if now - pos["last_update_time"] >= self.cooldown_seconds:
                                pos["current_sl"] = target_sl_price
                                pos["last_update_time"] = now
                                logger.info(f"[Trailing Short] [Compte: {pos['ctid_account_id']}] Déplacement du SL (Pos: {pos_id}) : {target_sl_price:.5f} (Distance: {self.trail_step*100}% derrière le bas à {current_price:.5f})")
                                self.send_amend_sl_request(pos['ctid_account_id'], pos_id, target_sl_price)

        except Exception as e:
            logger.error(f"[Trailing Tick Error] Erreur de traitement du tick de prix : {str(e)}")

    def subscribe_to_spots(self, ctid_trader_account_id, symbol_id):
        """Envoie une requête ProtoOASubscribeSpotsReq pour recevoir les flux de prix temps réel."""
        try:
            try:
                from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASubscribeSpotsReq
            except ImportError:
                ProtoOASubscribeSpotsReq = None

            if ProtoOASubscribeSpotsReq is None:
                return

            msg = ProtoOASubscribeSpotsReq()
            msg.ctidTraderAccountId = int(ctid_trader_account_id)
            msg.symbolId.append(int(symbol_id))
            
            if self.bridge and self.bridge.client:
                self.bridge.client.send(msg)
                logger.info(f"[Spots Subscribe] [Compte: {ctid_trader_account_id}] Abonnement demandé pour le symbole ID : {symbol_id}")
        except Exception as e:
            logger.error(f"[Spots Subscribe Error] Impossible de s'abonner aux spots : {str(e)}")

    def unsubscribe_from_spots(self, ctid_trader_account_id, symbol_id):
        """Envoie une requête ProtoOAUnsubscribeSpotsReq pour couper le flux inutile."""
        try:
            try:
                from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAUnsubscribeSpotsReq
            except ImportError:
                ProtoOAUnsubscribeSpotsReq = None

            if ProtoOAUnsubscribeSpotsReq is None:
                return

            msg = ProtoOAUnsubscribeSpotsReq()
            msg.ctidTraderAccountId = int(ctid_trader_account_id)
            msg.symbolId.append(int(symbol_id))
            
            if self.bridge and self.bridge.client:
                self.bridge.client.send(msg)
                logger.info(f"[Spots Unsubscribe] [Compte: {ctid_trader_account_id}] Désabonnement demandé pour le symbole ID : {symbol_id}")
        except Exception as e:
            logger.error(f"[Spots Unsubscribe Error] Impossible de se désabonner des spots : {str(e)}")

    def send_amend_sl_request(self, ctid_trader_account_id, position_id, sl_price: float):
        """Formate et transmet la requête de mise à jour du SL au serveur Spotware avec Price Offsetting (Camouflage)."""
        try:
            try:
                from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAAmendPositionSLTPReq
            except ImportError:
                ProtoOAAmendPositionSLTPReq = None

            if ProtoOAAmendPositionSLTPReq is None:
                return

            # Appliquer un Price Offsetting (bruit stochastique de décalage de prix de 0.01% à 0.05% selon l'ID du compte)
            offset_percentage = random.uniform(0.0001, 0.0005)
            offset_sign = 1 if (int(ctid_trader_account_id) % 2 == 0) else -1
            sl_price_offset = sl_price * (1.0 + (offset_sign * offset_percentage))

            msg = ProtoOAAmendPositionSLTPReq()
            msg.ctidTraderAccountId = int(ctid_trader_account_id)
            msg.positionId = int(position_id)
            
            # Formatage avec arrondissement strict pour éviter les rejets Spotware
            msg.stopLoss = round(float(sl_price_offset), 5)

            if self.bridge and self.bridge.client:
                self.bridge.client.send(msg)
                logger.info(f"[Trailing Request] [Compte: {ctid_trader_account_id}] [Camouflage Price Offset: {offset_sign * offset_percentage * 100:.4f}%] ProtoOAAmendPositionSLTPReq transmis (Pos ID: {position_id}, SL original: {sl_price:.5f}, SL bruité: {msg.stopLoss:.5f})")
        except Exception as e:
            logger.error(f"[Trailing Request Error] Impossible de modifier le SL sur le serveur : {str(e)}")
