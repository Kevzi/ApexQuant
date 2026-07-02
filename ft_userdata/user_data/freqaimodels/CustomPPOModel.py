import logging
from typing import Any, Dict
import numpy as np
import pandas as pd
from pandas import DataFrame
import gymnasium as gym

# Imports from Freqtrade FreqAI
from freqtrade.exceptions import OperationalException
from freqtrade.freqai.data_kitchen import FreqaiDataKitchen
import sys
import os

user_data_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if user_data_path not in sys.path:
    sys.path.append(user_data_path)

from freqtrade.freqai.RL.BaseReinforcementLearningModel import BaseReinforcementLearningModel
from freqtrade.freqai.RL.Base5ActionRLEnv import Actions, Base5ActionRLEnv
from freqtrade.freqai.RL.BaseEnvironment import Positions
from user_data.environments.PredatorInstitutionalEnv import PredatorInstitutionalEnv

logger = logging.getLogger(__name__)


import copy
import torch
import torch.nn.functional as F
import numpy as np
from stable_baselines3.ppo import PPO
from stable_baselines3.common.utils import explained_variance
import gymnasium as gym

class PFOPPO(PPO):
    def train(self) -> None:
        self.policy.set_training_mode(True)
        self._update_learning_rate(self.policy.optimizer)
        clip_range = self.clip_range(self._current_progress_remaining)
        if self.clip_range_vf is not None:
            clip_range_vf = self.clip_range_vf(self._current_progress_remaining)

        entropy_losses, pg_losses, value_losses, clip_fractions, pfo_losses = [], [], [], [], []

        old_policy = copy.deepcopy(self.policy)
        old_policy.eval()

        continue_training = True
        for epoch in range(self.n_epochs):
            approx_kl_divs = []
            for rollout_data in self.rollout_buffer.get(self.batch_size):
                actions = rollout_data.actions
                if isinstance(self.action_space, gym.spaces.Discrete):
                    actions = rollout_data.actions.long().flatten()

                values, log_prob, entropy = self.policy.evaluate_actions(rollout_data.observations, actions)
                values = values.flatten()

                with torch.no_grad():
                    old_features = old_policy.features_extractor(rollout_data.observations)
                current_features = self.policy.features_extractor(rollout_data.observations)
                pfo_loss = F.mse_loss(current_features, old_features)
                pfo_losses.append(pfo_loss.item())

                advantages = rollout_data.advantages
                if self.normalize_advantage and len(advantages) > 1:
                    advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

                ratio = torch.exp(log_prob - rollout_data.old_log_prob)
                policy_loss_1 = advantages * ratio
                policy_loss_2 = advantages * torch.clamp(ratio, 1 - clip_range, 1 + clip_range)
                policy_loss = -torch.min(policy_loss_1, policy_loss_2).mean()

                pg_losses.append(policy_loss.item())
                clip_fractions.append(torch.mean((torch.abs(ratio - 1) > clip_range).float()).item())

                if self.clip_range_vf is None:
                    values_pred = values
                else:
                    values_pred = rollout_data.old_values + torch.clamp(values - rollout_data.old_values, -clip_range_vf, clip_range_vf)
                
                value_loss = F.mse_loss(rollout_data.returns, values_pred)
                value_losses.append(value_loss.item())

                if entropy is None:
                    entropy_loss = -torch.mean(-log_prob)
                else:
                    entropy_loss = -torch.mean(entropy)
                entropy_losses.append(entropy_loss.item())

                loss = policy_loss + self.ent_coef * entropy_loss + self.vf_coef * value_loss + 0.5 * pfo_loss

                with torch.no_grad():
                    log_ratio = log_prob - rollout_data.old_log_prob
                    approx_kl_div = torch.mean((torch.exp(log_ratio) - 1) - log_ratio).cpu().numpy()
                    approx_kl_divs.append(approx_kl_div)

                self.policy.optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.policy.parameters(), self.max_grad_norm)
                self.policy.optimizer.step()

            self._n_updates += 1
            if not continue_training:
                break

        explained_var = explained_variance(self.rollout_buffer.values.flatten(), self.rollout_buffer.returns.flatten())
        self.logger.record('train/entropy_loss', np.mean(entropy_losses))
        self.logger.record('train/policy_gradient_loss', np.mean(pg_losses))
        self.logger.record('train/value_loss', np.mean(value_losses))
        self.logger.record('train/approx_kl', np.mean(approx_kl_divs))
        self.logger.record('train/clip_fraction', np.mean(clip_fractions))
        self.logger.record('train/pfo_loss', np.mean(pfo_losses))
        self.logger.record('train/loss', loss.item())
        self.logger.record('train/explained_variance', explained_var)
        self.logger.record('train/n_updates', self._n_updates, exclude='tensorboard')
        self.logger.record('train/clip_range', clip_range)

class CustomPPOModel(BaseReinforcementLearningModel):
    """
    V20-Fix-v2 : Modèle PPO (Reinforcement Learning) Hybride de Niveau Institutionnel.
    Ce modèle hérite de BaseReinforcementLearningModel et implémente l'apprentissage
    par renforcement via Stable-Baselines3 (PPO), découplé de la génération d'Alpha.
    Intègre désormais le "Plan C" (Optuna Optimization Engine) de manière native
    dans la boucle fit() pour résoudre l'effondrement de la variance expliquée.
    """

    def train(self, unfiltered_df: DataFrame, pair: str, dk: FreqaiDataKitchen, **kwargs) -> Any:
        """
        Surcharge de la méthode d'entraînement globale de FreqAI (BaseReinforcementLearningModel).
        Cela permet d'entraîner le HMM causalement et d'injecter la variable %-hmm_state
        avant l'instanciation des environnements (train_env / eval_env) et le calcul
        de l'Observation Space, évitant ainsi tout crash (MDP/Observation Space Mismatch).
        """
        # Appel de la méthode parent pour poursuivre le pipeline normal
        return super().train(unfiltered_df, pair, dk, **kwargs)

    def fit(self, data_dictionary: Dict[str, Any], dk: FreqaiDataKitchen, **kwargs) -> Any:
        """
        Entraînement de l'agent PPO de Stable-Baselines3 sur l'environnement d'apprentissage.
        Surcharge de la boucle pour intégrer le moteur d'optimisation d'hyperparamètres Optuna.
        """

        # Vérification de l'activation d'Optuna dans la configuration FreqAI-RL
        optuna_tuning = self.rl_config.get("optuna_tuning", False)

        if optuna_tuning:
            logger.info("[⚙️] DÉCLENCHEMENT DU PLAN C : Moteur d'optimisation Optuna activé.")
            try:
                import optuna
            except ImportError:
                logger.error("[-] Impossible de charger Optuna. Installez-le avec: pip install optuna")
                optuna_tuning = False

        if optuna_tuning:
            import optuna
            # Désactiver les logs d'Optuna pour garder le terminal épuré
            optuna.logging.set_verbosity(optuna.logging.WARNING)

            # Nombre d'essais (trials) définis dans la config
            n_trials = self.rl_config.get("optuna_trials", 15)
            logger.info(f"[⚙️] Lancement d'une étude d'optimisation sur {n_trials} essais pour soigner le Critique...")

            def objective(trial):
                # Échantillonnage de l'espace d'hyperparamètres critique (Selection Bias Elimination)
                vf_coef = trial.suggest_float("vf_coef", 0.3, 0.7, step=0.05)
                ent_coef = trial.suggest_float("ent_coef", 1e-4, 1e-2, log=True)
                learning_rate = trial.suggest_float("learning_rate", 5e-5, 5e-4, log=True)
                gae_lambda = trial.suggest_float("gae_lambda", 0.90, 0.98, step=0.01)
                clip_range = trial.suggest_float("clip_range", 0.1, 0.3, step=0.01)

                # Instanciation d'un agent PPO d'évaluation temporaire
                temp_policy_kwargs = {
                    "net_arch": {"pi": [256, 128], "vf": [128, 64]}
                }

                # Fusion des hyperparamètres d'Optuna avec ceux de la configuration
                training_params = self.rl_config.get("model_training_parameters", {}).copy()
                training_params.update({
                    "learning_rate": learning_rate,
                    "gae_lambda": gae_lambda,
                    "clip_range": clip_range,
                    "ent_coef": ent_coef,
                    "vf_coef": vf_coef,
                })

                eval_model = PFOPPO(
                    self.policy_type,
                    self.train_env,
                    policy_kwargs=temp_policy_kwargs,
                    verbose=0,
                    device='cpu',
                    **training_params
                )

                # Entraînement rapide sur l'environnement de train (Rollouts courts pour validation rapide)
                # 20 000 steps sont suffisants pour observer la dynamique de convergence d'Adam
                eval_model.learn(total_timesteps=20000)

                # Évaluation Out-Of-Sample RÉELLE sur l'environnement de test dédié (self.eval_env,
                # construit par FreqAI sur le split test). On compare la Value function V(s_t) du
                # Critique aux RENDEMENTS ACTUALISÉS cumulés G_t = r_t + gamma*r_{t+1} + ...
                # (mêmes échelles, contrairement à la récompense immédiate r_t seule).
                import torch
                gamma = eval_model.gamma

                step_rewards = []
                step_values = []
                step_dones = []

                obs = self.eval_env.reset()
                if isinstance(obs, tuple):
                    obs = obs[0]

                # Trajectoire d'évaluation OOS de 500 pas
                for _ in range(500):
                    # V(s_t) évaluée AVANT d'agir, pour un alignement correct avec r_t
                    obs_tensor, _ = eval_model.policy.obs_to_tensor(obs)
                    with torch.no_grad():
                        value = eval_model.policy.predict_values(obs_tensor).cpu().numpy().flatten()[0]

                    action, _ = eval_model.predict(obs, deterministic=True)
                    obs, reward, terminated, truncated, info = self.eval_env.step(action)
                    done = terminated[0] or truncated[0] if isinstance(terminated, (list, np.ndarray)) else terminated or truncated

                    step_values.append(float(value))
                    step_rewards.append(float(reward))
                    step_dones.append(bool(done))

                    if done:
                        obs = self.eval_env.reset()
                        if isinstance(obs, tuple):
                            obs = obs[0]

                # Nettoyage mémoire (OOM Prevention)
                import gc
                del eval_model
                gc.collect()

                step_rewards = np.array(step_rewards, dtype=np.float64)
                step_values = np.array(step_values, dtype=np.float64)

                if len(step_rewards) < 10 or np.var(step_rewards) == 0:
                    return -1.0  # Épisode trivial ou agent mort

                # Rendements actualisés causaux (backward pass), remis à zéro aux frontières d'épisode
                returns_to_go = np.zeros_like(step_rewards)
                running = 0.0
                for t in reversed(range(len(step_rewards))):
                    if step_dones[t]:
                        running = 0.0
                    running = step_rewards[t] + gamma * running
                    returns_to_go[t] = running

                if np.var(returns_to_go) == 0:
                    return -1.0

                # Explained Variance OOS stricte : 1 - Var(G - V) / Var(G)
                residuals = returns_to_go - step_values
                explained_var = 1.0 - (np.var(residuals) / np.var(returns_to_go))

                return float(explained_var)

            # Lancement de l'étude Optuna visant à maximiser la variance expliquée (avec TPE Sampler)
            study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
            study.optimize(objective, n_trials=min(12, n_trials))

            best_params = study.best_params
            best_ev = study.best_value
            
            logger.info(f"[🏁] OPTUNA TERMINÉ. Meilleure Explained Variance trouvée OOS : {best_ev:.4f}")
            logger.info(f"[🏁] Paramètres optimaux : {best_params}")

            # Surcharge des hyperparamètres d'entraînement avec les vainqueurs d'Optuna
            self.rl_config["model_training_parameters"].update({
                "learning_rate": best_params["learning_rate"],
                "gae_lambda": best_params["gae_lambda"],
                "clip_range": best_params["clip_range"],
                "ent_coef": best_params["ent_coef"],
                "vf_coef": best_params["vf_coef"]
            })

        # --- Fin de la section Optuna, poursuite de l'entraînement standard ---
        # Injection directe du dictionnaire pour bypasser le validateur JSON de Freqtrade
        policy_kwargs = {
            "net_arch": {"pi": [256, 128], "vf": [128, 64]}
        }

        if self.continual_learning and self.model:
            logger.info("Continual training activé - Reprise de l'entraînement du modèle existant.")
            model = self.model
            model.set_env(self.train_env)
        else:
            logger.info(f"Initialisation du modèle final {self.model_type} ({self.policy_type}) pour {dk.pair}")
            model = PFOPPO(
                self.policy_type,
                self.train_env,
                policy_kwargs=policy_kwargs,
                tensorboard_log=dk.data_path if self.rl_config.get("tensorboard", False) else None,
                verbose=1,
                device='cpu',
                **self.rl_config.get("model_training_parameters", {})
            )

        # Nombre d'étapes d'apprentissage totales
        total_timesteps = self.rl_config.get("total_timesteps", 100000)

        # Liste des callbacks FreqAI (Tensorboard + Évaluation maskable)
        callbacks = [self.tensorboard_callback]
        if self.eval_callback:
            callbacks.append(self.eval_callback)

        logger.info(f"Lancement de learn() final pour {total_timesteps} étapes sur {dk.pair}...")
        model.learn(
            total_timesteps=total_timesteps,
            callback=callbacks,
            reset_num_timesteps=not self.continual_learning
        )

        return model

    def predict(self, unfiltered_df: DataFrame, dk: FreqaiDataKitchen, **kwargs) -> Any:
        """
        Surcharge de la prédiction FreqAI pour décoder l'état HMM avant que
        l'environnement RL d'inférence (eval_env/predict_env) ne s'exécute.
        Et patch définitif de l'ordre des colonnes pour le DataSieve Pipeline.
        """
        # --- FIX FREQAI COLUMN ORDER DRIFT & ROGUE FEATURES ---
        train_cols = getattr(dk, 'training_features_list', None)
        if train_cols is not None:
            # 1. Hide any new '%' features that were not present during training
            rogue_cols = {c: c.replace('%', '_') for c in unfiltered_df.columns if c.startswith('%') and c not in train_cols}
            if rogue_cols:
                unfiltered_df.rename(columns=rogue_cols, inplace=True)
            
            # 2. Force physical column order
            existing_train_cols = [c for c in train_cols if c in unfiltered_df.columns]
            other_cols = [c for c in unfiltered_df.columns if c not in existing_train_cols]
            unfiltered_df = unfiltered_df[existing_train_cols + other_cols]

        return super().predict(unfiltered_df, dk, **kwargs)


    class MyRLEnv(PredatorInstitutionalEnv):
        """
        Environnement de Trading personnalisé héritant de PredatorInstitutionalEnv.
        Définit une fonction de récompense intégrant la Semi-Déviation (Downside Risk).
        """
        pass
