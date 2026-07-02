# 🦅 ApexQuant - Institutional Quantitative Architecture (V20)

ApexQuant est un système de trading algorithmique institutionnel asymétrique déployé sur les marchés à terme cryptographiques. Il est spécialement conçu pour surperformer les environnements hautement non-stationnaires tout en respectant les contraintes strictes des "Prop Firms" (comme Lark Funding).

Le système repose sur un moteur d'Apprentissage par Renforcement Profond (PPO) propulsé par FreqAI, couplé à une ingénierie de pointe (Plan B et Plan C) inspirée des travaux de Marcos López de Prado (Advances in Financial Machine Learning).

---

## 🏗️ Architecture du Système

### 1. Le Moteur de Features (Plan B)
L'intelligence de ce modèle ne réside pas dans son réseau de neurones, mais dans son espace d'observation stationnaire. Les prix bruts du marché ne sont jamais fournis à l'agent.
*   **Signaux Alpha LGBM :** Réduction de la dimensionnalité via PCA pour éliminer la colinéarité.
*   **Stationnarité SADF (Z-Score) :** Les tendances macro (EMA) sont passées au filtre SADF (Standardized Augmented Dickey-Fuller) pour compresser les marchés extrêmes (Bull Runs ou Flash Crashes) dans une distribution gaussienne digeste pour l'IA, empêchant le "Representation Collapse" du Critique.
*   **Asymétrie OBI (Order Book Imbalance) :** Prise en compte de la microstructure du marché (volume ask vs bid) pour le déclenchement chirurgical des entrées.

### 2. Le Moteur d'Optimisation Bayésien (Plan C - Optuna)
Afin d'éviter la paralysie cognitive de l'agent (le "Syndrome du Rentier" lors du premier entraînement sur Avalanche), le système intègre l'algorithme d'échantillonnage de Parzen (TPE) d'Optuna.
En cas d'échec sur une fenêtre temporelle, l'optimiseur corrige génétiquement la trajectoire :
*   Injection d'Entropie (`ent_coef`) pour forcer l'exploration.
*   Ajustement du Poids de la Fonction de Valeur (`vf_coef`) pour réparer le réseau de neurones.

### 3. Exécution et Gestion du Risque (Bouclier HRP)
Pour s'adapter aux règles strictes des Prop Firms (Maximum Drawdown de 10% sur Lark Funding) :
*   **Hierarchical Risk Parity (HRP) :** Construit un portefeuille diversifié utilisant la théorie des graphes (Clustering Hiérarchique) pour écraser la covariance sans nécessiter l'inversion d'une matrice.
*   **Pont cTrader Open API :** Traduction instantanée des signaux Freqtrade locaux vers des ordres FIX/API sur l'infrastructure institutionnelle Spotware.

---

## 📊 Performances de Validation (Out-of-Sample)
*Période de Backtest : Avril 2023 - Octobre 2023 (Bear Market Latéral)*
*   **Profit Net (CAGR) :** ~10.69%
*   **Win Rate :** ~39.7% (Asymétrie de Payout de type Trend Following).
*   **Meilleur Actif :** Solana (`SOL/USDT`) à +24.50%

---

## ⚙️ Déploiement

### Prérequis
- `python 3.10+`
- `freqtrade` (avec l'extension FreqAI)
- `cTrader Open API Application` (Validée par Spotware).

### Arborescence
```text
ApexQuant/
├── ft_userdata/                 # Le coeur Freqtrade (Configuration, IA, Poids Tensoriels)
├── infrastructure/
│   ├── execution/               # cTrader Bridge (Exécution asynchrone des signaux)
│   ├── portfolio/               # Allocateur HRP (Prop Firm Shield)
│   └── monitoring/              # Extracteur Tensorboard
└── alpha_research/              # Pipelines de création de features (LightGBM, DataSieve)
```

> "La rentabilité d'un modèle algorithmique n'est pas déterminée par sa précision directionnelle, mais par la robustesse mathématique de la gestion de ses échecs."
