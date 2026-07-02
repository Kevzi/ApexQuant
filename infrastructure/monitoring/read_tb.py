#!/usr/bin/env python3
"""
Moteur d'Extraction de Métriques Tensorboard - Predator V20
Conçu pour lire et compiler à la milliseconde les logs d'apprentissage PPO de FreqAI-RL.

Optimisations apportées :
1. Protection de la RAM VPS via 'size_guidance' (évite le chargement des images/histogrammes).
2. Détection dynamique des chemins d'exécution et fallback robuste.
3. Exportation optionnelle au format JSON pour l'intégration de pipelines de monitoring.
"""

import os
import glob
import json
import sys
import argparse
from typing import Optional, Dict, Any

try:
    from tensorboard.backend.event_processing import event_accumulator
except ImportError:
    print("[-] Erreur : 'tensorboard' est introuvable. Exécutez : pip install tensorboard")
    sys.exit(1)

def extract_latest_ppo_metrics(base_path: str, verbose: bool = True) -> Optional[Dict[str, Any]]:
    # Normalisation du chemin d'accès
    base_path = os.path.expanduser(base_path)
    if not os.path.exists(base_path):
        if verbose:
            print(f"[-] Erreur : Le chemin '{base_path}' n'existe pas.")
        return None

    # Recherche récursive des répertoires de runs PPO
    all_ppo_dirs = glob.glob(os.path.join(base_path, "*", "PPO_*"))
    if not all_ppo_dirs:
        # Fallback de recherche de premier niveau au cas où la structure serait plane
        all_ppo_dirs = glob.glob(os.path.join(base_path, "PPO_*"))
        if not all_ppo_dirs:
            if verbose:
                print("[-] Aucun dossier d'entraînement PPO trouvé dans l'arborescence.")
            return None

    # Sélection du dossier le plus récemment modifié
    dirs_sorted = sorted(all_ppo_dirs, key=os.path.getmtime, reverse=True)
    latest_dir = dirs_sorted[0]
    
    # Résolution des noms
    run_name = os.path.basename(latest_dir)
    pair_name = os.path.basename(os.path.dirname(latest_dir)) if len(os.path.dirname(latest_dir)) > len(base_path) else "Default_Pair"

    if verbose:
        print(f"[+] Dossier identifié : {pair_name} / {run_name}")

    # Recherche des fichiers d'événements tfevents
    event_files = glob.glob(os.path.join(latest_dir, "events.out.tfevents.*"))
    if not event_files:
        if verbose:
            print("[-] Erreur : Aucun fichier d'événements tfevents trouvé dans le dossier ciblé.")
        return None

    # Sélection du tfevent le plus récent
    event_files_sorted = sorted(event_files, key=os.path.getmtime, reverse=True)
    target_event_file = event_files_sorted[0]

    # OPTIMISATION CRITIQUE : Configuration du guide de taille pour éviter l'épuisement de la RAM (OOM)
    # On désactive le chargement des données lourdes (images, histogrammes complexes) dont nous n'avons pas besoin.
    size_guidance = {
        event_accumulator.COMPRESSED_HISTOGRAMS: 0,
        event_accumulator.IMAGES: 0,
        event_accumulator.AUDIO: 0,
        event_accumulator.SCALARS: 10,  # Ne conserve que les 10 dernières valeurs pour les scalaires
        event_accumulator.HISTOGRAMS: 0,
    }

    # Instanciation de l'accumulateur avec sa guidance
    acc = event_accumulator.EventAccumulator(target_event_file, size_guidance=size_guidance)
    
    try:
        acc.Reload()
    except Exception as e:
        if verbose:
            print(f"[-] Échec du rechargement de l'accumulateur d'événements : {str(e)}")
        return None

    # Récupération de la liste des tags de scalaires disponibles
    available_tags = acc.Tags().get('scalars', [])

    def get_latest_scalar(tag_name: str) -> Optional[float]:
        if tag_name in available_tags:
            scalars = acc.Scalars(tag_name)
            if scalars:
                return float(scalars[-1].value)
        return None

    # Extraction des indicateurs d'évaluation
    metrics = {
        "metadata": {
            "pair": pair_name,
            "run": run_name,
            "file": os.path.basename(target_event_file),
            "last_updated": os.path.getmtime(target_event_file)
        },
        "metrics": {
            "ep_rew_mean": get_latest_scalar("rollout/ep_rew_mean"),
            "ep_len_mean": get_latest_scalar("rollout/ep_len_mean"),
            "approx_kl": get_latest_scalar("train/approx_kl"),
            "explained_variance": get_latest_scalar("train/explained_variance"),
            "loss": get_latest_scalar("train/loss")
        }
    }

    return metrics

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extracteur de métriques Tensorboard pour Predator V20.")
    parser.add_argument(
        "--path", 
        type=str, 
        default="/mnt/c/Users/kevin/Downloads/Projet code/ft_userdata/user_data/models/bybit-futures-predator-v20-plan-b-15m/tensorboard/",
        help="Chemin de base contenant les dossiers Tensorboard."
    )
    parser.add_argument("--json", action="store_true", help="Retourne le résultat uniquement au format JSON épuré.")
    args = parser.parse_args()

    results = extract_latest_ppo_metrics(args.path, verbose=not args.json)
    
    if results:
        if args.json:
            print(json.dumps(results, indent=4))
        else:
            print("\n" + "="*50)
            print(f"📈 RAPPORT DE CONVERGENCE : {results['metadata']['pair']} ({results['metadata']['run']})")
            print("="*50)
            print(f"📍 Source : {results['metadata']['file']}")
            print("-"*50)
            m = results["metrics"]
            print(f"• ep_rew_mean (Récompense Moyenne) : {m['ep_rew_mean'] if m['ep_rew_mean'] is not None else 'N/A'}")
            print(f"• ep_len_mean (Durée de Survie)    : {m['ep_len_mean'] if m['ep_len_mean'] is not None else 'N/A'} bougies")
            print(f"• approx_kl (Divergence KL)       : {m['approx_kl'] if m['approx_kl'] is not None else 'N/A'}")
            print(f"• explained_variance (Critique)   : {m['explained_variance'] if m['explained_variance'] is not None else 'N/A'}")
            print(f"• loss (Fonction de perte globale): {m['loss'] if m['loss'] is not None else 'N/A'}")
            print("="*50 + "\n")
    else:
        sys.exit(1)
