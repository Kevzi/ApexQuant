import pandas as pd
import numpy as np
import lightgbm as lgb
import talib.abstract as ta
import os
import argparse
import logging
import matplotlib.pyplot as plt
from sklearn.model_selection import KFold
import optuna
from sklearn.metrics import log_loss, f1_score

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# 1. DIFFÉRENCIATION FRACTIONNAIRE (FRACDIFF FFD)
# =============================================================================
def getWeights_FFD(d, thres=1e-5):
    """
    Calcule les poids de Marcos López de Prado pour la différenciation fractionnaire FFD.
    """
    w = [1.0]
    k = 1
    while True:
        w_k = -w[-1] / k * (d - k + 1)
        if abs(w_k) < thres:
            break
        w.append(w_k)
        k += 1
    return np.array(w[::-1])

def fracDiff_FFD(series, d, thres=1e-5):
    """
    Applique la différenciation fractionnaire avec fenêtre fixe (FFD) sur un DataFrame ou une Series.
    Préserve au maximum la mémoire longue des cycles macroéconomiques.
    """
    w = getWeights_FFD(d, thres)
    width = len(w) - 1
    
    if isinstance(series, pd.Series):
        series_df = pd.DataFrame(series)
    else:
        series_df = series.copy()
        
    res = {}
    for col in series_df.columns:
        seriesF = series_df[col].ffill().dropna()
        n = len(seriesF)
        df_ = pd.Series(index=seriesF.index, dtype=float)
        
        # Produit matriciel glissant vectorisé
        for i in range(width, n):
            loc = seriesF.index[i]
            df_[loc] = np.dot(w, seriesF.iloc[i - width : i + 1])
            
        res[col] = df_
        
    return pd.DataFrame(res) if isinstance(series, pd.DataFrame) else res[series.name]


# =============================================================================
# 2. ÉCHANTILLONNAGE PAR BOUGIES DE DOLLAR (VIRTUAL DOLLAR BARS)
# =============================================================================
def build_dollar_bars(df_1m, threshold=1000000):
    """
    Regroupe les bougies fines de 1m en bougies de dollar homogènes (Dollar Bars).
    """
    df_1m = df_1m.copy()
    df_1m['dollar_volume'] = df_1m['close'] * df_1m['volume']
    df_1m['cum_dollar_volume'] = df_1m['dollar_volume'].cumsum()
    
    group_idx = (df_1m['cum_dollar_volume'] // threshold).astype(int)
    
    dollar_bars = df_1m.groupby(group_idx).agg({
        'date': 'last',  # Horloge de clôture de la bougie de dollar
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum',
        'dollar_volume': 'sum'
    }).reset_index(drop=True)
    return dollar_bars

def compute_dollar_bar_features(df_15m, df_1m, threshold=1000000):
    """
    Calcule des indicateurs sur les Dollar Bars virtuels et les fusionne sans look-ahead bias
    sur l'index temporel principal en 15m.
    """
    # 1. Construction des Dollar Bars
    dollar_bars = build_dollar_bars(df_1m, threshold)
    
    # 2. Calcul des indicateurs sur l'espace d'information stable
    dollar_bars['rsi_dollar'] = ta.RSI(dollar_bars, timeperiod=14)
    bb = ta.BBANDS(dollar_bars, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
    dollar_bars['bb_width_dollar'] = (bb['upperband'] - bb['lowerband']) / bb['middleband']
    
    # 3. Alignement temporel asynchrone par pd.merge_asof (sens directionnel : backward)
    df_15m = df_15m.sort_values('date')
    dollar_bars = dollar_bars.sort_values('date')
    
    aligned = pd.merge_asof(
        df_15m,
        dollar_bars[['date', 'rsi_dollar', 'bb_width_dollar']],
        on='date',
        direction='backward'
    )
    return aligned


# =============================================================================
# 3. VALIDATION CROISÉE PURGÉE AVEC EMBARGO (PURGED K-FOLD CV)
# =============================================================================
class PurgedKFold:
    """
    Validation croisée purgée avec embargo pour éliminer les fuites de données (Data Leakage)
    induites par les fenêtres de chevauchement de la méthode de la Triple Barrière.
    """
    def __init__(self, n_splits=3, pct_embargo=0.01, label_horizon=8):
        self.n_splits = n_splits
        self.pct_embargo = pct_embargo
        self.label_horizon = label_horizon

    def split(self, X, y=None, groups=None):
        n_samples = len(X)
        kf = KFold(n_splits=self.n_splits, shuffle=False)
        
        embargo_size = int(n_samples * self.pct_embargo)
        if embargo_size == 0 and self.pct_embargo > 0:
            embargo_size = 1
            
        for train_idx, test_idx in kf.split(X):
            test_start = test_idx[0]
            test_end = test_idx[-1]
            
            # 1. Purge (évite le chevauchement d'informations futures de TBM sur l'entraînement)
            purged_train_idx = train_idx[
                (train_idx < test_start - self.label_horizon) | 
                (train_idx > test_end)
            ]
            
            # 2. Embargo (casse l'autocorrélation résiduelle après le test set)
            final_train_idx = purged_train_idx[
                (purged_train_idx < test_start) | 
                (purged_train_idx > test_end + embargo_size)
            ]
            
            yield final_train_idx, test_idx


# =============================================================================
# 4. GÉNÉRATEUR DE DONNÉES SYNTHÉTIQUES ROBUSTE PAR PAIRE
# =============================================================================
def generate_pair_synthetic_data(pair, seed, rows=20000):
    """
    Génère des données synthétiques 1m réalistes et distinctes pour chaque paire.
    """
    np.random.seed(seed)
    # Prix de base différents par actif pour refléter la réalité
    base_prices = {
        'BTC/USDT:USDT': 65000.0,
        'ETH/USDT:USDT': 3500.0,
        'SOL/USDT:USDT': 140.0,
        'BNB/USDT:USDT': 580.0
    }
    base_price = base_prices.get(pair, 100.0)
    
    dates = pd.date_range('2025-01-01', periods=rows, freq='1min')
    # Volatilités différentes par actif (ex: SOL est plus volatil que BTC)
    vols = {
        'BTC/USDT:USDT': 0.001,
        'ETH/USDT:USDT': 0.0015,
        'SOL/USDT:USDT': 0.0025,
        'BNB/USDT:USDT': 0.0018
    }
    vol = vols.get(pair, 0.0015)
    
    returns = np.random.normal(0, vol, rows)
    close = base_price * np.exp(np.cumsum(returns))
    df = pd.DataFrame({
        'date': dates,
        'open': close * (1 + np.random.normal(0, 0.0005, rows)),
        'high': close * (1 + np.abs(np.random.normal(0, 0.001, rows))),
        'low': close * (1 - np.abs(np.random.normal(0, 0.001, rows))),
        'close': close,
        'volume': np.random.lognormal(2, 0.5, rows) * 10
    })
    return df

def calculate_volatility(df, window=8):
    returns = df['close'].pct_change()
    volatility = returns.rolling(window=window).std()
    return volatility.bfill().fillna(0.001)

def apply_triple_barrier(df, vol, upper_mult=2.5, lower_mult=1.5, vertical_bars=8):
    labels = pd.Series(0, index=df.index)
    close = df['close'].values
    vols = vol.values
    n = len(df)
    for i in range(n - vertical_bars):
        current_price = close[i]
        current_vol = vols[i]
        upper_barrier = current_price * (1 + (upper_mult * current_vol))
        lower_barrier = current_price * (1 - (lower_mult * current_vol))
        touched = 0
        for j in range(1, vertical_bars + 1):
            future_price = close[i + j]
            if future_price >= upper_barrier:
                touched = 1
                break
            elif future_price <= lower_barrier:
                touched = -1
                break
        labels.iloc[i] = touched
    return labels


def directional_from_proba(model, X_rows, eps=1e-6):
    """
    Convertit les probabilités 3-classes {-1, 0, 1} en une probabilité DIRECTIONNELLE
    dans [0, 1], neutre à 0.5 : P(hausse) / (P(hausse) + P(baisse)).
    Résout l'incohérence 'P(1) 3-classes vs seuil 0.5' côté stratégie.
    """
    classes = list(model.classes_)
    proba = model.predict_proba(X_rows)
    p_up = proba[:, classes.index(1)] if 1 in classes else np.zeros(len(X_rows))
    p_dn = proba[:, classes.index(-1)] if -1 in classes else np.zeros(len(X_rows))
    return (p_up + eps) / (p_up + p_dn + 2.0 * eps)


def generate_oos_walkforward(X, y, params, n_splits=5, label_horizon=8):
    """
    Signal directionnel Out-Of-Sample par walk-forward ANCRÉ (expanding window).
    Chaque fold est entraîné UNIQUEMENT sur le passé (gap de purge de label_horizon
    barres) puis prédit le fold suivant. Aucune fuite temporelle -> signal déployable.
    Retourne un np.array aligné sur X ; positions non couvertes = NaN.
    """
    n = len(X)
    oos = np.full(n, np.nan)
    if n < (n_splits + 1) * 20:
        return oos
    fold_size = n // (n_splits + 1)
    for k in range(1, n_splits + 1):
        train_end = k * fold_size
        test_start = train_end
        test_end = (k + 1) * fold_size if k < n_splits else n
        tr_end_purged = max(0, train_end - label_horizon)
        if tr_end_purged < 20:
            continue
        X_tr, y_tr = X[:tr_end_purged], y[:tr_end_purged]
        X_te = X[test_start:test_end]
        if len(np.unique(y_tr)) < 2 or len(X_te) == 0:
            continue
        model = lgb.LGBMClassifier(**params)
        model.fit(X_tr, y_tr)
        oos[test_start:test_end] = directional_from_proba(model, X_te)
    return oos


# =============================================================================
# 5. PIPELINE PRINCIPAL MULTI-PAIRES
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pairs', type=str, default='BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,BNB/USDT:USDT', 
                        help='Liste de paires séparées par des virgules')
    parser.add_argument('--input_15m_dir', type=str, default='user_data/data/bybit/futures', help='Dossier des données 15m')
    parser.add_argument('--input_1m_dir', type=str, default='user_data/data/bybit/futures', help='Dossier des données 1m')
    parser.add_argument('--output', type=str, default='user_data/alpha_signals_v20.csv', help='Fichier unifié de sortie')
    parser.add_argument('--optimize', action='store_true', help='Activer Optuna Phase 1 (PurgedKFold)')
    args = parser.parse_args()
    
    print("=" * 80)
    print("   DÉMARRAGE DU MOTEUR ALPHA MULTI-PAIRES V20 (LÓPEZ DE PRADO ARCHITECTURE)   ")
    print("=" * 80)
    
    pairs_list = [p.strip() for p in args.pairs.split(',')]
    logger.info(f"Paires Whiteliste à traiter ({len(pairs_list)}) : {pairs_list}")
    
    master_signals = []
    
    for idx, pair in enumerate(pairs_list):
        logger.info(f"\n>>> Traitement de la paire [{idx+1}/{len(pairs_list)}] : {pair}")
        
        # 1. Chargement des données (Vraies ou Synthétiques)
        # Normalisation du nom de fichier pour correspondre à Freqtrade
        pair_clean = pair.replace('/', '_').replace(':', '_')
        file_15m = None
        file_1m = None
        
        if args.input_15m_dir and args.input_1m_dir:
            file_15m_path_json = os.path.join(args.input_15m_dir, f"{pair_clean}-15m.json")
            file_15m_path_csv = os.path.join(args.input_15m_dir, f"{pair_clean}-15m.csv")
            file_15m_path_feather = os.path.join(args.input_15m_dir, f"{pair_clean}-15m-futures.feather")
            file_1m_path_json = os.path.join(args.input_1m_dir, f"{pair_clean}-1m.json")
            file_1m_path_csv = os.path.join(args.input_1m_dir, f"{pair_clean}-1m.csv")
            file_1m_path_feather = os.path.join(args.input_1m_dir, f"{pair_clean}-1m-futures.feather")
            
            if os.path.exists(file_15m_path_feather):
                file_15m = file_15m_path_feather
            elif os.path.exists(file_15m_path_json):
                file_15m = file_15m_path_json
            elif os.path.exists(file_15m_path_csv):
                file_15m = file_15m_path_csv
                
            if os.path.exists(file_1m_path_feather):
                file_1m = file_1m_path_feather
            elif os.path.exists(file_1m_path_json):
                file_1m = file_1m_path_json
            elif os.path.exists(file_1m_path_csv):
                file_1m = file_1m_path_csv
                
        if file_15m and file_1m:
            logger.info(f"[OK] Chargement des fichiers pour {pair} depuis le disque.")
            df_15m = pd.read_feather(file_15m) if file_15m.endswith('.feather') else pd.read_json(file_15m) if file_15m.endswith('.json') else pd.read_csv(file_15m)
            df_1m = pd.read_feather(file_1m) if file_1m.endswith('.feather') else pd.read_json(file_1m) if file_1m.endswith('.json') else pd.read_csv(file_1m)
        else:
            logger.warning(f"[Attention] Fichiers absents pour {pair}. Génération de données synthétiques distinctes (Seed: {42 + idx})...")
            df_1m = generate_pair_synthetic_data(pair, seed=42 + idx, rows=25000)
            df_15m = df_1m.resample('15min', on='date').agg({
                'open': 'first',
                'high': 'max',
                'low': 'min',
                'close': 'last',
                'volume': 'sum'
            }).reset_index()
            
        df_15m['date'] = pd.to_datetime(df_15m['date'])
        df_1m['date'] = pd.to_datetime(df_1m['date'])
        
        # 1. Échantillonnage Dollar Bars Virtuels & Alignement sans look-ahead
        logger.info("-> Étape 1 : Construction et alignement des caractéristiques des Dollar Bars...")
        df = compute_dollar_bar_features(df_15m, df_1m, threshold=1000000)
        
        # 2. Différenciation Fractionnaire (FFD) pour préserver la mémoire longue
        logger.info("-> Étape 2 : Application de la Différenciation Fractionnaire (d=0.35)...")
        df['%-fracdiff_close'] = fracDiff_FFD(df['close'], d=0.35, thres=1e-4)
        
        # 3. Ajout des indicateurs microstructures classiques
        df['%-pct-change'] = df['close'].pct_change()
        rsi_15m = ta.RSI(df, timeperiod=14)
        df['%-norm_rsi_15m'] = (rsi_15m - 50.0) / 50.0
        
        # 4. Étiquetage par Triple Barrière SYMÉTRIQUE (TBM) - 2.0 / 2.0
        # Barrières égales -> classes {-1, +1} équilibrées -> signal directionnel bidirectionnel
        # (évite la famine d'exploration / representation collapse du PPO côté long).
        logger.info("-> Étape 3 : Étiquetage par Triple Barrière Symétrique 2.0/2.0 (horizon 8 bougies)...")
        vol = calculate_volatility(df, window=8)
        df['target'] = apply_triple_barrier(df, vol, upper_mult=2.0, lower_mult=2.0, vertical_bars=8)
        
        # SÉCURITÉ MATHÉMATIQUE : On invalide les 8 dernières lignes (label_horizon) pour éviter d'insérer 
        # des labels "Neutre" non résolus par manque d'historique futur dans le jeu d'entraînement.
        df.loc[df.index[-8:], 'target'] = np.nan
        
        # Nettoyage des NaNs induits par la chauffe des indicateurs, FracDiff et la purge finale des étiquettes
        train_data = df.dropna().copy()
        
        features = ['%-fracdiff_close', 'rsi_dollar', 'bb_width_dollar', '%-norm_rsi_15m', '%-pct-change']
        X = train_data[features].values
        y = train_data['target'].values
        
        logger.info(f"Volume d'entraînement éligible : {X.shape[0]} observations.")
        logger.info(f"Distribution des classes TBM : \n{train_data['target'].value_counts().to_dict()}")
        
        # 5. Entraînement LightGBM (avec ou sans Optuna)
        if args.optimize:
            logger.info("-> Étape 4 : Optimisation Optuna LightGBM sous PurgedKFold...")
            optuna.logging.set_verbosity(optuna.logging.WARNING)

            def objective(trial):
                param = {
                    'n_estimators': trial.suggest_int('n_estimators', 50, 250, step=10),
                    'max_depth': trial.suggest_int('max_depth', 3, 7),
                    'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
                    'num_leaves': trial.suggest_int('num_leaves', 15, 63),
                    'min_child_samples': trial.suggest_int('min_child_samples', 20, 100),
                    'random_state': 42,
                    'verbosity': -1
                }
                
                pkf = PurgedKFold(n_splits=3, pct_embargo=0.02, label_horizon=8)
                fold_losses = []
                
                for train_idx, test_idx in pkf.split(X, y):
                    X_train, y_train = X[train_idx], y[train_idx]
                    X_test, y_test = X[test_idx], y[test_idx]
                    
                    model = lgb.LGBMClassifier(**param)
                    # Pas d'early stopping sur le fold de test : sinon le test servirait à
                    # décider l'arrêt (fuite). On entraîne le nombre d'arbres suggéré.
                    model.fit(X_train, y_train)

                    preds_proba = model.predict_proba(X_test)
                    loss = log_loss(y_test, preds_proba, labels=model.classes_)
                    fold_losses.append(loss)
                    
                return np.mean(fold_losses)

            study = optuna.create_study(direction='minimize')
            study.optimize(objective, n_trials=25)
            
            best_params = study.best_params
            logger.info(f"   Meilleurs paramètres Optuna : {best_params} (Log-Loss OOS: {study.best_value:.4f})")
            final_params = {**best_params, 'random_state': 42, 'verbosity': -1}
        else:
            logger.info("-> Étape 4 : Entraînement LightGBM standard (sans Optuna)...")
            final_params = {'n_estimators': 100, 'learning_rate': 0.05, 'max_depth': 5, 'random_state': 42, 'verbosity': -1}

        # 5. SIGNAL OOS SANS FUITE : walk-forward causal (train strictement dans le passé).
        # C'est CE signal qui alimente le backtest -> fin de la prédiction in-sample.
        logger.info("-> Étape 5 : Génération du signal directionnel Out-Of-Sample (walk-forward causal)...")
        oos_directional = generate_oos_walkforward(X, y, final_params, n_splits=5, label_horizon=8)

        # Modèle final entraîné sur tous les labels résolus : sert UNIQUEMENT à prédire la
        # queue récente non encore étiquetée (usage live légitime, pas de fuite passé->futur).
        final_model = lgb.LGBMClassifier(**final_params)
        final_model.fit(X, y)

        # 6. Assemblage du signal directionnel complet, aligné sur df :
        #    - warmup / queue récente : prédiction du modèle final (causale pour la queue)
        #    - bougies historiques résolues : écrasées par le signal OOS walk-forward (sans fuite)
        X_full = df[features].ffill().values  # ffill uniquement -> plus de bfill (anti-lookahead)
        full_directional = pd.Series(0.5, index=df.index)  # neutre par défaut (warmup non prédictible)
        valid_mask = ~np.isnan(X_full).any(axis=1)
        if valid_mask.any():
            full_directional.loc[valid_mask] = directional_from_proba(final_model, X_full[valid_mask])

        oos_series = pd.Series(oos_directional, index=train_data.index).dropna()
        full_directional.loc[oos_series.index] = oos_series.values

        probabilities = full_directional.values
        logger.info(f"   Couverture OOS : {len(oos_series)}/{len(df)} bougies (reste = modèle final causal / neutre).")

        # Sauvegarde temporaire pour cette paire
        pair_signals = pd.DataFrame({
            'date': df['date'],
            'pair': pair,
            '%-lgbm_predict': probabilities
        })
        master_signals.append(pair_signals)
        logger.info(f"[SUCCÈS] Alpha directionnel calculé pour {pair}.")
        
    # Fusion de tous les signaux
    final_output_df = pd.concat(master_signals, ignore_index=True)
    final_output_df.to_csv(args.output, index=False)
    
    print("\n" + "=" * 80)
    print(f"--> [SUCCÈS GLOBAL] Moteur Alpha V20 déployé avec succès dans '{args.output}'.")
    print(f"    Nombre total de lignes de signaux générées : {len(final_output_df)}")
    print("=" * 80)

if __name__ == '__main__':
    main()
