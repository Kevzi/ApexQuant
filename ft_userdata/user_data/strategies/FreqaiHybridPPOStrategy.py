import logging
import os
import json
import zmq
from functools import reduce
from datetime import datetime
from typing import Optional
import numpy as np
import pandas as pd
from pandas import DataFrame
import talib.abstract as ta
from statsmodels.tsa.stattools import adfuller

# Freqtrade imports
from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade

logger = logging.getLogger(__name__)

class FreqaiHybridPPOStrategy(IStrategy):
    """
    V20 : FreqaiHybridPPOStrategy-v7 (Le "Predator" Multi-Timeframe Multi-Paires en 15m).
    
    Cette stratégie fusionne le meilleur des deux mondes :
    1. La génération d'Alpha directionnel brute par paire (gérée par LightGBM de manière 
       indépendante et injectée de manière étanche via %-lgbm_predict).
    2. La gestion de l'exécution et de l'allocation temporelle de capital par l'agent PPO.
    3. L'intégration dynamique à chaud (Hot-Reloading) du Sizing HRP (Hierarchical Risk Parity)
       via la lecture asynchrone et sécurisée par cache de 'hrp_allocations.csv'.
    """

    INTERFACE_VERSION = 3
    timeframe = '15m'
    startup_candle_count = 400

    # Risque et Exécution Bybit Futures
    can_short = True
    stoploss = -0.15  # Stoploss global d'urgence ("Circuit Breaker") à -15%
    use_custom_stoploss = True
    trailing_stop = False
    position_adjustment_enable = False

    # Variables de cache internes pour HRP et Narrative
    _hrp_weights = {}
    _hrp_last_mtime = 0
    _last_narrative_log = {}

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # RESEARCH MODE : diagnostic uniquement. Quand Kelly<=0, on planche le risque
        # (0.5%) au lieu d'abstenir (stake=0), pour évaluer la perf BRUTE de l'alpha
        # sur tout l'échantillon. NE JAMAIS activer en prod (piloté par config overlay).
        self.research_mode = bool(config.get('research_mode', False))
        if self.research_mode:
            logger.warning("[RESEARCH MODE] ACTIF : abstention Kelly désactivée (plancher 0.5%). Diagnostic uniquement, PAS pour la prod.")
        # Initialisation du pipeline ZMQ vers cTrader
        self.zmq_context = zmq.Context()
        self.zmq_socket = self.zmq_context.socket(zmq.PUSH)
        self.zmq_socket.connect("tcp://127.0.0.1:5555")
        logger.info("[ZMQ PUSH] Connexion établie au Bridge cTrader sur le port 5555 pour l'Agent PPO")

    # -------------------------------------------------------------------------
    # CALL BACKS INITIALISATION & HOT-RELOAD DE L'HRP
    # -------------------------------------------------------------------------
    def bot_start(self, **kwargs) -> None:
        """
        Appelé une fois au démarrage de la stratégie par Freqtrade.
        Initialise les variables d'allocation HRP.
        """
        self._hrp_weights = {}
        self._hrp_last_mtime = 0
        self.load_hrp_allocations()

    def bot_loop_start(self, **kwargs) -> None:
        """
        Appelé au début de chaque itération de boucle de Freqtrade.
        Permet de recharger dynamiquement à chaud (Hot-Reloading) les nouveaux poids HRP
        si le script 'hrp_allocator.py' s'est ré-exécuté en tâche de fond.
        """
        self.load_hrp_allocations()

    def load_hrp_allocations(self) -> None:
        """
        Charge et parse le fichier 'hrp_allocations.csv' de manière sécurisée.
        Filtre et mémorise les poids en RAM pour éviter les lectures de disque intempestives.
        """
        # Chemins de recherche ordonnés pour s'adapter à tous les environnements d'exécution
        paths_to_try = [
            'hrp_allocations.csv',
            'user_data/hrp_allocations.csv',
            'user_data/notebooks/hrp_allocations.csv',
            '/workspace/hrp_allocations.csv',
            '/workspace/artifacts/hrp_allocations.csv'
        ]
        
        file_found = None
        for path in paths_to_try:
            if os.path.exists(path):
                file_found = path
                break
                
        if not file_found:
            return

        try:
            mtime = os.path.getmtime(file_found)
            # Ne recharge que si le fichier a été modifié sur le disque
            if mtime > self._hrp_last_mtime:
                df = pd.read_csv(file_found)
                # Supprime les espaces des colonnes
                df.columns = [c.strip() for c in df.columns]
                
                weights = {}
                for _, row in df.iterrows():
                    strat_name = str(row.iloc[0]).strip()
                    # La deuxième colonne contient le poids HRP (%)
                    weight_val = float(row.iloc[1]) / 100.0  # Conversion en ratio (ex: 14.24% -> 0.1424)
                    weights[strat_name] = weight_val
                    
                self._hrp_weights = weights
                self._hrp_last_mtime = mtime
                logger.info(f"[HRP] Rechargement à chaud réussi depuis {file_found}. Poids : {self._hrp_weights}")
        except Exception as e:
            logger.error(f"[HRP] Erreur critique lors de la lecture du fichier HRP : {str(e)}")

    # -------------------------------------------------------------------------
    # GESTION DU LEVIER
    # -------------------------------------------------------------------------
    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, side: str, **kwargs) -> float:
        """
        Force un levier de x3 sur les positions Long et Short en marge isolée.
        """
        return 3.0

    # -------------------------------------------------------------------------
    # STOP-LOSS DYNAMIQUE (BREAKEVEN)
    # -------------------------------------------------------------------------
    def custom_stoploss(self, pair: str, trade: 'Trade', current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        """
        Stop-loss dynamique pour verrouiller un Breakeven à partir de +1.5% de profit.
        """
        # Si le profit dépasse +1.5%, on remonte le SL à +0.15% (Breakeven + frais/spread)
        if current_profit >= 0.015:
            from freqtrade.strategy import stoploss_from_open
            
            # Log de l'action dans le Narrative Journal (une seule fois par trade)
            if not hasattr(self, '_breakeven_triggered'):
                self._breakeven_triggered = {}
                
            if not self._breakeven_triggered.get(trade.id, False):
                narrative = f"[{current_time}] [{pair}] 🔵 SÉCURISATION (Breakeven). Profit latent à +{current_profit*100:.2f}%. Stop-Loss ajusté à +0.2% (risk-free trade)."
                log_path = os.path.join('user_data', 'logs', 'ai_narrative.log')
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(narrative + "\n")
                self._breakeven_triggered[trade.id] = True
                
            return stoploss_from_open(0.0015, current_profit, is_short=trade.is_short)

        # Sous +2%, on conserve le Stop-Loss d'urgence (circuit breaker -12%).
        # NB : renvoyer -1 placerait le SL à -100% (désactivé). On renvoie le plancher dur.
        return self.stoploss

    # -------------------------------------------------------------------------
    # PROTECTIONS (Global Kill-Switches)
    # -------------------------------------------------------------------------
    @property
    def protections(self):
        return [
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 48,
                "trade_limit": 10,
                "stop_duration_candles": 288,  # Pause de 24h en cas de panique
                "max_allowed_drawdown": 0.20   # Drawdown maximum de 20%
            }
        ]

    # -------------------------------------------------------------------------
    # FEATURES FreqAI (Engineering & expansions)
    # -------------------------------------------------------------------------
    def feature_engineering_expand_all(self, dataframe: DataFrame, period: int, metadata: dict, **kwargs) -> DataFrame:
        """
        Indicateurs techniques de base soumis à auto-expansion (indicator_periods_candles).
        """
        dataframe[f'%-rsi-{period}'] = ta.RSI(dataframe, timeperiod=period)
        dataframe[f'%-ema-{period}'] = ta.EMA(dataframe, timeperiod=period)
        dataframe[f'%-sma-{period}'] = ta.SMA(dataframe, timeperiod=period)
        dataframe[f'%-atr-{period}'] = ta.ATR(dataframe, timeperiod=period)
        
        # Bandes de Bollinger et BB Width
        bb = ta.BBANDS(dataframe, timeperiod=period, nbdevup=2.0, nbdevdn=2.0)
        dataframe[f'%-bb_lower-{period}'] = bb['lowerband']
        dataframe[f'%-bb_middle-{period}'] = bb['middleband']
        dataframe[f'%-bb_upper-{period}'] = bb['upperband']
        dataframe[f'%-bb_width-{period}'] = (bb['upperband'] - bb['lowerband']) / bb['middleband']
        
        # Volume relatif et Z-Score de Volume
        dataframe[f'%-volume_mean-{period}'] = dataframe['volume'].rolling(period).mean()
        dataframe[f'%-volume_zscore-{period}'] = (dataframe['volume'] - dataframe[f'%-volume_mean-{period}']) / dataframe['volume'].rolling(period).std().replace(0, 0.00001)
        dataframe[f'%-momentum_roc-{period}'] = ta.ROC(dataframe, timeperiod=period)

        return dataframe

    def feature_engineering_expand_basic(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        """
        Indicateurs avancés non dépendants des périodes.
        """
        dataframe['%-pct-change'] = dataframe['close'].pct_change()
        dataframe['%-raw_volume'] = dataframe['volume']

        # -------------------------------------------------------------------------
        # PLAN B - MICROSTRUCTURE FEATURES (Injecté suite au collapse sur SOL)
        # -------------------------------------------------------------------------
        # 1. ATR Normalisé (Volatilité brute stationnaire)
        dataframe['%-norm_atr'] = ta.ATR(dataframe, timeperiod=14) / dataframe['close']
        
        # 2. OBI Proxy (Order Book Imbalance par Bulk Volume Classification)
        range_hl = dataframe['high'] - dataframe['low']
        range_hl = range_hl.replace(0, 0.00001)
        dataframe['%-micro_obi_proxy'] = (dataframe['close'] - dataframe['low']) / range_hl
        
        # 3. Proxy de Stationnarité SADF (Z-Score glissant sur 60 périodes vectorisé)
        rolling_mean = dataframe['close'].rolling(window=60).mean()
        rolling_std = dataframe['close'].rolling(window=60).std()
        dataframe['%-adf_stat_proxy'] = (dataframe['close'] - rolling_mean) / rolling_std.replace(0, 0.00001)

        # 4. HMM Dynamic State (Causal Training on Current Rolling Window)
        try:
            from hmmlearn.hmm import GaussianHMM
            raw_close = dataframe['close'].values
            log_returns = np.diff(np.log(raw_close))
            log_returns = np.insert(log_returns, 0, 0)
            X = log_returns.reshape(-1, 1)
            
            # Fast causal fit on the current rolling dataframe (training window or live 400 candles)
            hmm_model = GaussianHMM(n_components=2, covariance_type="diag", n_iter=50, random_state=42)
            hmm_model.fit(X)
            states = hmm_model.predict(X)
            
            var_state_0 = np.var(X[states == 0]) if len(X[states == 0]) > 0 else 0
            var_state_1 = np.var(X[states == 1]) if len(X[states == 1]) > 0 else 0
            chaotic_state = 1 if var_state_1 > var_state_0 else 0
            
            dataframe['%-hmm_state'] = np.where(states == chaotic_state, 1, 0)
        except Exception as e:
            logger.error(f"[HMM] Dynamic Fit Error: {e}")
            dataframe['%-hmm_state'] = 0


        # -----------------------------------------------------------------
        # 5. CHAINING / LGBM ALPHA MODEL (In-Strategy Read with temporal & pair alignment)
        # -----------------------------------------------------------------
        paths_to_try = [
            'alpha_signals_v20.csv',
            'alpha_signals.csv',
            '../alpha_signals.csv',
            'user_data/alpha_signals_v20.csv',
            'user_data/alpha_signals.csv',
            'user_data/notebooks/alpha_signals_v20.csv',
            '/workspace/alpha_signals_v20.csv',
            '/workspace/scratch/alpha_signals_v20.csv',
            '/workspace/artifacts/alpha_signals_v20.csv'
        ]
        
        current_pair = metadata.get('pair', '')
        alpha_df = None
        latest_date = dataframe['date'].iloc[-1]
        
        import time
        max_retries = 36  # Wait up to 180 seconds (36 * 5s)
        retry_delay = 5
        
        if self.dp and getattr(self.dp, 'runmode', None) and self.dp.runmode.value == 'backtest':
            max_retries = 1
            retry_delay = 0
        
        for attempt in range(max_retries):
            for path in paths_to_try:
                if os.path.exists(path):
                    try:
                        raw_alpha_df = pd.read_csv(path)
                        raw_alpha_df['date'] = pd.to_datetime(raw_alpha_df['date'])
                        
                        if 'pair' in raw_alpha_df.columns:
                            temp_df = raw_alpha_df[raw_alpha_df['pair'] == current_pair].copy()
                        else:
                            temp_df = raw_alpha_df.copy()
                            
                        if not temp_df.empty:
                            csv_latest = temp_df['date'].max()
                            df_latest = latest_date
                            
                            # Normalize timezones for comparison
                            if csv_latest.tzinfo is not None:
                                csv_latest = csv_latest.tz_localize(None)
                            if df_latest.tzinfo is not None:
                                df_latest = df_latest.tz_localize(None)
                                
                            # Le CSV contient les bougies complètes.
                            # La bougie actuelle de Freqtrade est en cours de formation (ex: 14h30).
                            # Le CSV max_date sera donc la bougie précédente (ex: 14h15).
                            target_date = df_latest - pd.Timedelta(minutes=15)
                            
                            if csv_latest >= target_date:
                                alpha_df = temp_df
                                logger.info(f"[LGBM Load] Alignement étanche réussi pour {current_pair} (T={attempt*5}s)")
                                break
                    except Exception as e:
                        pass
                        
            if alpha_df is not None:
                break
                
            logger.info(f"[LGBM Sync] Attente du pipeline Cron pour {current_pair} (tentative {attempt+1}/{max_retries})...")
            time.sleep(retry_delay)
            
        if alpha_df is None:
            logger.warning(f"[LGBM Timeout] Le CSV n'a pas été mis à jour à temps. Utilisation des anciennes données pour {current_pair}.")
            # On relit la dernière version disponible en fallback
            for path in paths_to_try:
                if os.path.exists(path):
                    try:
                        raw_alpha_df = pd.read_csv(path)
                        raw_alpha_df['date'] = pd.to_datetime(raw_alpha_df['date'])
                        if 'pair' in raw_alpha_df.columns:
                            alpha_df = raw_alpha_df[raw_alpha_df['pair'] == current_pair].copy()
                        else:
                            alpha_df = raw_alpha_df.copy()
                        break
                    except Exception:
                        pass
        
        if alpha_df is not None and not alpha_df.empty:
            # Aligne sans look-ahead bias par index temporel
            dataframe = pd.merge(dataframe, alpha_df[['date', '%-lgbm_predict']], on='date', how='left')
            dataframe['%-lgbm_predict'] = dataframe['%-lgbm_predict'].ffill().fillna(0.5)
            
            # SAUVEGARDE EN RAM POUR LE NARRATIVE LOGGER (Évite la purge de FreqAI)
            if not hasattr(self, '_last_lgbm_predict'):
                self._last_lgbm_predict = {}
            self._last_lgbm_predict[current_pair] = float(dataframe['%-lgbm_predict'].iloc[-1])
        else:
            # Fallback sécurisé (probabilité neutre)
            dataframe['%-lgbm_predict'] = 0.5
            if not hasattr(self, '_last_lgbm_predict'):
                self._last_lgbm_predict = {}
            self._last_lgbm_predict[current_pair] = 0.5

        return dataframe

    def feature_engineering_standard(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        """
        Features standards et assignation obligatoire des prix OHLC bruts.
        REQUIS par FreqAI pour le calcul de get_unrealized_profit() en entraînement.
        """
        dataframe["%-raw_close"] = dataframe["close"]
        dataframe["%-raw_open"] = dataframe["open"]
        dataframe["%-raw_high"] = dataframe["high"]
        dataframe["%-raw_low"] = dataframe["low"]
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour

        # --- FIX FREQAI ENGINE BUG (COLUMN ORDER) ---
        # Freqtrade's Live DataKitchen sometimes merges features in a slightly different order than the Backtest DataKitchen.
        # Sklearn Pipeline strictly requires the exact same column order. We force it here.
        if getattr(self, 'freqai', None) and getattr(self.freqai, 'dk', None):
            train_cols = getattr(self.freqai.dk, 'training_features_list', None)
            if train_cols is not None:
                existing_train_cols = [c for c in train_cols if c in dataframe.columns]
                other_cols = [c for c in dataframe.columns if c not in existing_train_cols]
                dataframe = dataframe[existing_train_cols + other_cols]

        return dataframe

    # ---------------------------------------------------------------------------------
    # TARGETS (Pour le Reinforcement Learner)
    # ---------------------------------------------------------------------------------
    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        """
        L'apprentissage par renforcement ne requiert pas de cible supervisée classique.
        Nous définissons le &-action requis par l'environnement.
        """
        dataframe["&-action"] = 0
        return dataframe

    # ---------------------------------------------------------------------------------
    # CUSTOM EXIT (Safety Net pour les positions Longues stagnantes)
    # ---------------------------------------------------------------------------------
    def custom_exit(self, pair: str, trade: 'Trade', current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs):
        """
        Sécurité temporelle pour couper les positions longues stagnantes en perte.
        """
        # On cible uniquement les positions LONG actuellement en perte
        if not trade.is_short and current_profit < 0:
            # Calcul de la durée en minutes depuis l'ouverture du trade
            trade_duration = (current_time - trade.open_date_utc).total_seconds() / 60
            
            # Si le trade est ouvert depuis plus de 3 heures (180 minutes)
            if trade_duration > 180:
                return "timeout_losing_long"
                
        return None

    # ---------------------------------------------------------------------------------
    # POPULATE INDICATORS
    # ---------------------------------------------------------------------------------
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Lancement de l'entraînement ou inférence de l'agent PPO
        dataframe = self.freqai.start(dataframe, metadata, self)
        
        # Recalcul manuel ultra-rapide des indicateurs pour le Narrative Logger (car FreqAI les purge de la RAM)
        try:
            rsi_series = ta.RSI(dataframe, timeperiod=14)
            vol_mean = dataframe['volume'].rolling(14).mean()
            vol_std = dataframe['volume'].rolling(14).std().replace(0, 0.00001)
            vol_z_series = (dataframe['volume'] - vol_mean) / vol_std
            atr_series = ta.ATR(dataframe, timeperiod=14) / dataframe['close']
            
            raw_rsi = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else 50.0
            raw_vol_z = float(vol_z_series.iloc[-1]) if not pd.isna(vol_z_series.iloc[-1]) else 0.0
            raw_atr = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
        except:
            raw_rsi, raw_vol_z, raw_atr = 50.0, 0.0, 0.0
            
        # Pour le LGBM, on le récupère instantanément du cache de la stratégie (sauvegardé dans feature_engineering_expand_basic)
        raw_lgbm = 0.5
        if hasattr(self, '_last_lgbm_predict'):
            raw_lgbm = self._last_lgbm_predict.get(metadata.get('pair', ''), 0.5)
        
        # --- EXPORT TELEMETRIE POUR LE DASHBOARD (Live IPC) ---
        import os
        import json
        from datetime import datetime
        try:
            if len(dataframe) > 0:
                last_row = dataframe.iloc[-1]
                pair = metadata.get('pair', '')
                
                # --- 1. TELEMETRIE BTC ---
                if 'BTC' in pair:
                    telemetry = {
                        "di": float(last_row.get("&-action", 0.0)),
                        "do_predict": int(last_row.get("do_predict", 1)),
                        "hmm_state": float(last_row.get("%-hmm_state", 0.0)),
                        "timestamp": datetime.now().isoformat()
                    }
                    telemetry_path = os.path.join('user_data', 'freqai_telemetry.json')
                    with open(telemetry_path, 'w') as f:
                        json.dump(telemetry, f)
                        
                # --- 2. AI NARRATIVE LOGGER (TRADUCTION HUMAINE) ---
                current_time = last_row['date']
                if self._last_narrative_log.get(pair) != current_time:
                    action = int(last_row.get("&-action", 0))
                    hmm = int(last_row.get("%-hmm_state", 0))
                    do_predict = int(last_row.get("do_predict", 0))
                    
                    # Restaurer les métriques sauvegardées
                    lgbm = raw_lgbm
                    rsi = raw_rsi
                    vol_z = raw_vol_z
                    atr = raw_atr
                    
                    narrative = f"[{current_time}] [{pair}] "
                    
                    if do_predict == 0:
                        narrative += "Décision : FILTRÉ (Outlier). La configuration de marché est inconnue ou trop extrême par rapport à l'entraînement. "
                        if vol_z > 2.0: narrative += f"Volume anormal (Z-Score: +{vol_z:.1f}). "
                        elif rsi > 70 or rsi < 30: narrative += f"RSI extrême ({rsi:.0f}). "
                        narrative += "Je refuse d'agir à l'aveugle."
                    elif hmm == 1:
                        narrative += f"Décision : OBSERVATION. Le régime de marché est identifié comme CHAOTIQUE (HMM=1). Volatilité: {atr:.4f}. Signaux coupés par sécurité."
                    else:
                        trades = Trade.get_trades_proxy(pair=pair, is_open=True)
                        has_trade = len(trades) > 0
                        is_long = has_trade and not trades[0].is_short
                        is_short = has_trade and trades[0].is_short
                        profit = trades[0].calc_profit_ratio(last_row['close']) * 100 if has_trade else 0.0
                        
                        if action == 1:
                            if is_long:
                                narrative += f"Décision : MAINTIEN LONG. Le signal haussier reste fort (Alpha LGBM: {lgbm:.1%}, RSI: {rsi:.0f}). PnL latent: {profit:.2f}%. L'Agent PPO conforte la position."
                            elif is_short:
                                narrative += f"Décision : CLÔTURE SHORT (REVERSE). Le marché se retourne à la hausse (Alpha LGBM: {lgbm:.1%}). L'Agent PPO coupe le Short d'urgence pour passer à l'achat !"
                            else:
                                narrative += f"Décision : ACHAT LONG ! Signal haussier fort (Alpha LGBM: {lgbm:.1%}, RSI: {rsi:.0f}). Volume (Z: {vol_z:.1f}). L'Agent PPO valide l'entrée."
                        elif action == 3:
                            if is_short:
                                narrative += f"Décision : MAINTIEN SHORT. Le signal baissier reste fort (Alpha LGBM: {(1-lgbm):.1%}, RSI: {rsi:.0f}). PnL latent: {profit:.2f}%. L'Agent PPO conforte la position."
                            elif is_long:
                                narrative += f"Décision : CLÔTURE LONG (REVERSE). Le marché se retourne violemment à la baisse (Alpha LGBM: {(1-lgbm):.1%}). L'Agent PPO coupe le Long d'urgence pour shorter !"
                            else:
                                narrative += f"Décision : VENTE SHORT ! Signal baissier fort (Alpha LGBM: {(1-lgbm):.1%}, RSI: {rsi:.0f}). Volume (Z: {vol_z:.1f}). L'Agent PPO valide l'entrée."
                        elif action == 2:
                            narrative += f"Décision : CLÔTURE LONG. Le momentum haussier s'épuise (LGBM: {lgbm:.1%}). L'Agent PPO sécurise la position."
                        elif action == 4:
                            narrative += f"Décision : CLÔTURE SHORT. Le momentum baissier s'épuise (LGBM remonté à {lgbm:.1%}). L'Agent PPO sécurise la position."
                        else:
                            # Action 0 = Hold Neutral / Wait
                            if has_trade:
                                direction = "LONG" if is_long else "SHORT"
                                narrative += f"Décision : MAINTIEN {direction}. Tendance saine. PnL latent: {profit:.2f}%. Je laisse courir."
                            else:
                                if lgbm > 0.5:
                                    narrative += f"Décision : ATTENTE. Biais légèrement haussier ({lgbm:.1%}) mais trop faible pour l'Agent PPO. (RSI: {rsi:.0f}). Je patiente."
                                else:
                                    narrative += f"Décision : ATTENTE. Biais légèrement baissier ({(1-lgbm):.1%}) mais trop faible pour l'Agent PPO. (RSI: {rsi:.0f}). Je patiente."
                    
                    log_path = os.path.join('user_data', 'logs', 'ai_narrative.log')
                    os.makedirs(os.path.dirname(log_path), exist_ok=True)
                    with open(log_path, 'a', encoding='utf-8') as f:
                        f.write(narrative + "\n")
                        
                    self._last_narrative_log[pair] = current_time

        except Exception as e:
            logger.debug(f"[Telemetry/Narrative] Echec export: {e}")

        return dataframe

    # ---------------------------------------------------------------------------------
    # CONDITIONS D'ENTRÉE ET DE SORTIE VIA ACTIONS PPO
    # ---------------------------------------------------------------------------------
    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        if "&-action" in df.columns:
            # 🟢 ENTREE LONG (Achat) : L'agent RL décide de prendre position à l'achat (1)
            df.loc[
                (
                    (df['do_predict'] == 1) & 
                    (df['&-action'] == 1) &
                    (df['volume'] > 0)
                ),
                ['enter_long', 'enter_tag']] = (1, 'rl_long')

            # 🔴 ENTREE SHORT (Vente à découvert) : L'agent RL décide de shorter (3)
            df.loc[
                (
                    (df['do_predict'] == 1) & 
                    (df['&-action'] == 3) &
                    (df['volume'] > 0)
                ),
                ['enter_short', 'enter_tag']] = (1, 'rl_short')
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        if "&-action" in df.columns:
            # 🟢 SORTIE LONG : L'agent RL décide de fermer le Long (2) OU de retourner sa veste en Short (3)
            df.loc[
                (
                    (df['do_predict'] == 1) & 
                    (df['&-action'].isin([2, 3]))
                ),
                'exit_long'] = 1

            # 🔴 SORTIE SHORT : L'agent RL décide de fermer le Short (4) OU de retourner sa veste en Long (1)
            df.loc[
                (
                    (df['do_predict'] == 1) & 
                    (df['&-action'].isin([4, 1]))
                ),
                'exit_short'] = 1
        return df

    # -------------------------------------------------------------------------
    # MONEY MANAGEMENT : DYNAMIC HRP ALLOCATION & STAKE SIZING (V20 MULTI-PAIR)
    # -------------------------------------------------------------------------
    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: Optional[float], max_stake: float,
                            entry_tag: Optional[str], **kwargs) -> float:
        """
        Sizing Half-Kelly dynamique.
        Le Kelly calcule le RISQUE optimal (plafonné à 1.5%).
        La taille de la position (Stake) est déduite du Risque et du Stop-Loss.
        """
        total_balance = proposed_stake
        if self.wallets:
            total_balance = self.wallets.get_total('USDT')
            
        trades = Trade.get_trades_proxy(is_open=False)
        if len(trades) >= 5:
            winning_trades = [t for t in trades if t.close_profit > 0]
            win_rate = len(winning_trades) / len(trades)
            avg_win = sum(t.close_profit for t in winning_trades) / len(winning_trades) if winning_trades else 0.01
            losing_trades = [t for t in trades if t.close_profit <= 0]
            avg_loss = abs(sum(t.close_profit for t in losing_trades) / len(losing_trades)) if losing_trades else 0.01
            risk_reward = avg_win / avg_loss if avg_loss > 0 else 1.0
            
            # Formule de Kelly (Pourcentage du capital à RISQUER)
            kelly_pct = win_rate - ((1.0 - win_rate) / risk_reward) if risk_reward > 0 else 0
            half_kelly_risk = kelly_pct / 2.0

            # Edge défavorable (Kelly <= 0) -> abstention totale : on ne trade pas.
            # EXCEPTION research_mode : on planche le risque à 0.5% pour diagnostiquer l'alpha
            # sur tout l'échantillon (sinon le sizing coupe le trading et on est aveugles).
            if half_kelly_risk <= 0:
                if self.research_mode:
                    logger.info("[Half-Kelly][RESEARCH] Edge négatif mais research_mode ON -> plancher risque 0.5%.")
                    risk_pct = 0.005
                else:
                    logger.info("[Half-Kelly] Edge négatif détecté (Kelly<=0). Abstention : stake=0.")
                    return 0.0
            else:
                # Plancher retiré (le risque peut descendre près de 0) ; plafond conservé à 1.5%.
                risk_pct = min(0.015, half_kelly_risk)
        else:
            # Risque par défaut si historique insuffisant : 1%
            risk_pct = 0.01
            
        # STAKE = RISQUE / STOP_LOSS
        # Si le Stop-Loss est à 15% (0.15), et le risque est de 1.5% (150$), 
        # le Stake sera de 150 / 0.15 = 1000$.
        stop_loss_abs = abs(self.stoploss) if hasattr(self, 'stoploss') else 0.15
        stake_pct = risk_pct / stop_loss_abs
            
        stake = total_balance * stake_pct
        stake = max(min_stake if min_stake else stake, min(max_stake, stake))
        logger.info(f"[Half-Kelly] Risque visé: {risk_pct:.2%} | StopLoss: {stop_loss_abs:.2%} | Stake calculé: {stake:.2f} USDT")
        return stake

    # -------------------------------------------------------------------------
    # EMISSION ZMQ VERS CTRADER BRIDGE (EXECUTION REELLE)
    # -------------------------------------------------------------------------
    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, 
                             time_in_force: str, current_time: datetime, entry_tag: Optional[str], side: str, **kwargs) -> bool:
        """Déclenche le pont cTrader juste avant de valider l'entrée simulée de Freqtrade."""
        direction = "LONG" if side == "long" else "SHORT"
        
        payload = {
            "action": "ENTRY",
            "pair": pair,
            "amount": amount,
            "price": rate,
            "direction": direction,
            "timestamp": current_time.isoformat(),
            "sl_percentage": self.stoploss,
            "tp_percentage": 0.10  # TP arbitraire ou dynamique si implémenté dans l'IA
        }
        try:
            self.zmq_socket.send_string(json.dumps(payload), zmq.NOBLOCK)
            logger.info(f"[PPO ZMQ PUSH] Ordre {direction} envoyé pour {pair} (Tags: {entry_tag})")
        except zmq.Again:
            logger.warning("[ZMQ WARNING] Le buffer vers cTrader est plein, message en attente.")
        return True

    def confirm_trade_exit(self, pair: str, trade: 'Trade', order_type: str, amount: float, 
                            rate: float, time_in_force: str, exit_reason: str, current_time: datetime, **kwargs) -> bool:
        """Déclenche la liquidation de la position sur cTrader."""
        # Autoriser TOUJOURS les sorties de secours pour protéger le drawdown de 5%
        if exit_reason in ("stop_loss", "force_exit", "emergency_exit", "trailing_stop_loss"):
            logger.info(f"[Conformité] Sortie de secours autorisée pour {pair} (Raison: {exit_reason})")
        else:
            # Appliquer le verrou de 120s uniquement pour les signaux standards de l'IA
            trade_duration = (current_time - trade.open_date_utc).total_seconds()
            if trade_duration < 120:
                logger.warning(f"[Conformité] Signal de sortie bloqué pour {pair} : seulement {trade_duration:.1f}s d'exposition (Requis: 120s)")
                return False
                
        direction = "LONG" if not trade.is_short else "SHORT"
        
        payload = {
            "action": "EXIT",
            "pair": pair,
            "amount": amount,
            "direction": direction,
            "timestamp": current_time.isoformat()
        }
        try:
            self.zmq_socket.send_string(json.dumps(payload), zmq.NOBLOCK)
            logger.info(f"[PPO ZMQ PUSH] Signal d'Exit envoyé pour {pair} (Raison: {exit_reason})")
        except zmq.Again:
            logger.critical(f"[ZMQ CRITICAL] Échec critique d'envoi du signal EXIT pour {pair} !")
        return True

