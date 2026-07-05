import numpy as np
import logging
import collections
import pandas as pd
from freqtrade.freqai.RL.Base5ActionRLEnv import Actions, Base5ActionRLEnv, Positions

logger = logging.getLogger(__name__)

class PredatorInstitutionalEnv(Base5ActionRLEnv):
    """
    V17 Institutional Environment for FreqAI.
    Features:
    1. Exponential Drawdown Penalty (FTMO compliant)
    2. Dynamic Action Masking based on Macro Trend
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.dd_threshold = 0.03  # Seuil de 3% pour la sécurité FTMO
        self.dd_alpha = 0.1
        self.dd_beta = 100.0
        self.returns_history = collections.deque(maxlen=288)
        self._peak_equity = 1.0
        self._last_equity = 1.0
        self._prev_unrealized = 0.0

    def reset(self, seed=None, **kwargs):
        """
        Réinitialise l'environnement ET l'état d'équité à chaque nouvel épisode.
        Sans ce reset, _peak_equity / _last_equity / returns_history conservaient l'état
        de l'épisode précédent -> pénalité de drawdown fantôme dès le step 0 et
        Downside-Deviation biaisée.
        """
        obs = super().reset(seed=seed)
        self._peak_equity = 1.0
        self._last_equity = 1.0
        self._prev_unrealized = 0.0
        self.returns_history.clear()
        return obs

    def calculate_reward(self, action: int) -> float:
        """
        Calcule la récompense avec une pénalité exponentielle de drawdown.
        """
        is_in_trade = self._position != Positions.Neutral
        current_profit = self.get_unrealized_profit()
        maker_fee = 0.0002
        taker_fee = 0.00055
        leverage = 3.0
        
        prev_unrealized = getattr(self, '_prev_unrealized', 0.0)
        is_exit_action = action in (Actions.Long_exit.value, Actions.Short_exit.value)

        raw_reward = 0.0
        if not is_in_trade:
            if action in (Actions.Long_enter.value, Actions.Short_enter.value):
                raw_reward -= maker_fee * leverage
            elif action == Actions.Neutral.value:
                raw_reward += 0.00001
        else:
            if is_exit_action:
                # Sortie : on réalise le PnL de façon SYMÉTRIQUE.
                # (L'ancienne pénalité ×2 sur les pertes apprenait à l'agent à CONSERVER
                #  ses positions perdantes plutôt que de les couper -> effet de disposition.)
                actual_profit = current_profit - (taker_fee * leverage)
                raw_reward += actual_profit
            else:
                # Maintien : récompense = variation du PnL latent sur ce pas (mark-to-market).
                # Laisser courir une perte est donc pénalisé en proportion de la perte,
                # et couper une perte n'est jamais plus coûteux que de la garder.
                raw_reward += (current_profit - prev_unrealized)

        # Mémorise le PnL latent pour le mark-to-market du prochain pas
        # (0.0 si on n'est pas/plus en position, pour repartir propre au prochain trade).
        self._prev_unrealized = current_profit if (is_in_trade and not is_exit_action) else 0.0
        
        # ⚠️ CORRECTION MAJEURE : calcul de l'équité sous forme absolue pour éviter d'avoir 100% de drawdown dès le départ
        # Ajouter 1.0 permet de reconstituer une courbe d'équité démarrant à 1.0.
        current_equity = 1.0 + self._total_profit + current_profit
        
        # Suivi des rendements pas à pas pour la Downside Deviation
        if not hasattr(self, '_last_equity'):
            self._last_equity = 1.0
            
        step_return = (current_equity - self._last_equity) / self._last_equity
        self._last_equity = current_equity
        self.returns_history.append(step_return)
        
        # Calcul de la Downside Deviation (Semi-déviation)
        returns_arr = np.array(self.returns_history)
        neg_returns = returns_arr[returns_arr < 0]
        sigma_down = np.sqrt(np.mean(neg_returns**2)) if len(neg_returns) > 0 else 0.0
        
        # Suivi du sommet d'équité historique (Peak Equity)
        if not hasattr(self, '_peak_equity') or current_equity > self._peak_equity:
            self._peak_equity = max(1.0, current_equity)
            
        peak_equity = self._peak_equity
        
        # Calcul du drawdown relatif temporaire
        dd_t = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0.0
        
        # Application de la pénalité exponentielle asymétrique si le drawdown dépasse le seuil
        penalty_dd = 0.0
        if dd_t > self.dd_threshold:
            penalty_dd = self.dd_alpha * np.exp(self.dd_beta * (dd_t - self.dd_threshold)) - self.dd_alpha
            penalty_dd = min(penalty_dd, 10.0)  # Capping de la pénalité pour éviter de saturer les gradients
            
        # Pénalité composite : Risk-Aware = (Rendement brut) - (w2 * Downside_Dev) - (w3 * Drawdown_Exp)
        # On log ces métriques via tensorboard_log si disponible (souvent injecté dynamiquement dans Freqtrade)
        if hasattr(self, 'tensorboard_log'):
            self.tensorboard_log("reward/downside_deviation", sigma_down, inc=False)
            self.tensorboard_log("reward/penalty_dd", penalty_dd, inc=False)
            
        return float(raw_reward - (sigma_down * 15.0) - penalty_dd)

    def action_masks(self) -> np.ndarray:
        """
        Masquage dynamique des actions basé sur le régime HMM (Hidden Markov Model).
        0 = Neutral, 1 = Long_enter, 2 = Long_exit, 3 = Short_enter, 4 = Short_exit
        """
        # Par défaut, Neutral, Long_exit et Short_exit sont toujours autorisés
        masks = [True, False, True, False, True] 
        
        hmm_cols = [c for c in self.raw_features.columns if 'hmm_state' in c]
        
        if hmm_cols:
            hmm_state = self.raw_features[hmm_cols[0]].iloc[self._current_tick]
        else:
            hmm_state = 0.0  # Régime Calme par défaut si non trouvé
            
        if hmm_state == 1.0:
            # Régime Toxique/Chaotique : Interdit formellement les entrées (Action Masking)
            masks[1] = False
            masks[3] = False
        else:
            # Régime Calme : Autorise les deux directions
            masks[1] = True
            masks[3] = True
            
        return np.array(masks)
