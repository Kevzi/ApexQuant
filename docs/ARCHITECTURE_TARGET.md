# Chantier « Structure » (futur, à ne PAS mélanger avec la réconciliation)

V2 conserve **volontairement** la structure actuelle (split `ft_userdata/` + `infrastructure/`,
chemins relatifs CWD-dépendants) pour tourner **à l'identique** du VPS. Ce document liste les
défauts structurels connus et la cible d'un refactor dédié ultérieur.

## Défauts actuels
1. **Double imbrication `ft_userdata/user_data/`** — la convention Freqtrade est un simple `user_data/`.
2. **Deux racines couplées par CWD** — le bridge se lance depuis la racine du repo, freqtrade depuis
   `ft_userdata/`. Les chemins relatifs changent selon le point de lancement (source de bugs d'analyse).
3. **Fichiers en vrac à la racine** (scripts, logs, .sqlite, .sh) — nettoyé dans V2 mais la structure reste.
4. **Chemins de fallback en dur** dans la stratégie (`/workspace/...`, `user_data/notebooks/...`) —
   résidus d'exécutions multi-environnements (notebook/cloud + VPS).
5. **`hrp_allocations.csv` à la racine de `ft_userdata/`** alors que la strat le cherche aussi dans
   `user_data/` et `user_data/notebooks/` — placement incohérent.

## Cible d'un refactor dédié
- Un seul `user_data/` à la racine (convention Freqtrade standard).
- Chemins pilotés par **config** (ou variables d'environnement), plus aucun chemin relatif CWD-dépendant
  ni `/workspace/...` en dur.
- Racine du repo sans fichiers en vrac (tout sous `user_data/`, `infrastructure/`, `docs/`, `scripts/`).
- `hrp_allocations.csv` à un emplacement unique et documenté.
- `start_*.sh` mis à jour en conséquence.

## Prérequis avant de lancer ce chantier
- Un V2 **validé en backtest** (parité de comportement avec le VPS) servant de référence.
- Tests de non-régression : même backtest avant/après refactor -> résultats identiques.
