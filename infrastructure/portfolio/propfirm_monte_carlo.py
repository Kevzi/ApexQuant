import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # Headless backend pour les environnements de serveur
import matplotlib.pyplot as plt
import os

def run_monte_carlo_simulation(num_paths=1000, months=12):
    """
    Simule la trajectoire de Stacking géométrique de comptes Prop Firm (Lark Funding)
    avec l'architecture V20-Fix (4% brut ciblé par mois, 75% taux de validation).
    """
    np.random.seed(42)  # Reproductibilité quantitative
    
    # Paramètres Lark Funding 3.0 & V20-Fix
    target_monthly_gross = 0.04  # 4% brut moyen mensuel cible (grâce à l'Alpha LGBM)
    monthly_volatility = 0.015   # Volatilité mensuelle stochastique du modèle
    profit_split = 0.90          # 90% de profit split (avec l'add-on activé)
    max_lark_allocation = 300000 # Plafond global d'allocation stochastique chez Lark
    
    # Grille de frais et challenges Lark (1-Step)
    challenge_catalog = {
        10000:   {"fee": 200,  "pass_prob": 0.80},  # Sandbox à faible risque
        25000:   {"fee": 300,  "pass_prob": 0.75},
        50000:   {"fee": 500,  "pass_prob": 0.75},
        100000:  {"fee": 800,  "pass_prob": 0.75},
        200000:  {"fee": 1500, "pass_prob": 0.70}
    }

    # Initialisation des matrices de résultats (chemins x mois)
    cash_matrix = np.zeros((num_paths, months + 1))
    capital_matrix = np.zeros((num_paths, months + 1))
    
    # Conditions initiales
    cash_matrix[:, 0] = 200.0  # Budget initial de 200$ (suffisant pour le challenge à 10k)
    capital_matrix[:, 0] = 10000.0  # Début en Phase 1 avec le compte de 10 000$

    # Statut du challenge en cours de validation pour chaque chemin : 
    # [Taille_du_challenge, Mois_de_debut, Frais_payes]
    pending_challenges = [None] * num_paths 
    refunds_due = np.zeros(num_paths)

    for m in range(1, months + 1):
        for p in range(num_paths):
            # Charger l'état actuel du chemin
            current_cash = cash_matrix[p, m - 1]
            current_capital = capital_matrix[p, m - 1]
            active_challenge = pending_challenges[p]
            
            # --- 1. Simulation de la performance de trading (Comptes financés actifs) ---
            earnings = 0.0
            if current_capital > 0:
                # Modélisation gaussienne du rendement mensuel du robot V20-Fix
                realized_return = np.random.normal(target_monthly_gross, monthly_volatility)
                # S'assurer que le drawdown d'urgence de la prop firm n'est pas touché (Max DD daily < 5%)
                if realized_return < -0.05:
                    realized_return = -0.05  # Coupe-circuit de la prop firm déclenché
                    # Probabilité marginale d'invalidation de compte (3% sous V20-Fix)
                    if np.random.rand() < 0.03:
                        current_capital -= 10000.0 # Perte d'une tranche de capital
                        current_capital = max(0.0, current_capital)
                
                # Calcul de la part des gains nets reversés (Profit Split)
                if realized_return > 0:
                    earnings = current_capital * realized_return * profit_split

            # --- 2. Traitement du challenge en cours ---
            challenge_passed = False
            if active_challenge is not None:
                size, start_month, fee = active_challenge
                # Modélisation de la durée de validation stochastique (généralement 1 mois)
                if m > start_month:
                    prob_pass = challenge_catalog[size]["pass_prob"]
                    if np.random.rand() < prob_pass:
                        # Succès ! Le capital est augmenté dès le mois suivant
                        challenge_passed = True
                        new_cap = current_capital + size
                        # Respect strict du plafond de Stacking de Lark Funding
                        if new_cap <= max_lark_allocation:
                            current_capital = new_cap
                            # Lark rembourse les frais du challenge lors du premier payout
                            current_cash += fee  
                        pending_challenges[p] = None
                    else:
                        # Échec : Les frais de challenge sont perdus, l'opérateur doit retenter
                        pending_challenges[p] = None

            # --- 3. Prise de décision géométrique (Achat de nouveaux challenges) ---
            # Si nous avons du cash libre et aucun challenge actif en cours de validation
            if pending_challenges[p] is None and current_capital < max_lark_allocation:
                # Stratégie d'échelle : Choisir le challenge le plus gros achetable avec le cash disponible
                eligible_sizes = [size for size, data in challenge_catalog.items() if current_cash >= data["fee"]]
                if eligible_sizes:
                    best_size = max(eligible_sizes)
                    # S'assurer que le Stacking ne dépasse pas le plafond Lark de 300k
                    if current_capital + best_size <= max_lark_allocation:
                        fee = challenge_catalog[best_size]["fee"]
                        current_cash -= fee
                        pending_challenges[p] = (best_size, m, fee)

            # Mise à jour des matrices de résultats
            cash_matrix[p, m] = current_cash + earnings
            capital_matrix[p, m] = current_capital

    return cash_matrix, capital_matrix

def generate_report_and_plots(cash, capital, months=12):
    """
    Analyse les trajectoires de Monte-Carlo, génère un rapport statistique complet
    et exporte un graphique de projection d'équité.
    """
    # Calcul des percentiles pour le cash disponible
    p5_cash = np.percentile(cash, 5, axis=0)
    p50_cash = np.percentile(cash, 50, axis=0)
    p95_cash = np.percentile(cash, 95, axis=0)
    
    # Calcul des percentiles pour le capital financé sous gestion
    p50_cap = np.percentile(capital, 50, axis=0)
    p95_cap = np.percentile(capital, 95, axis=0)

    # 1. Génération du tableau récapitulatif (Chemin Médian - 50e percentile)
    months_range = range(months + 1)
    df_results = pd.DataFrame({
        "Mois": months_range,
        "Capital Financé Actif ($)": p50_cap.astype(int),
        "Cash Libre Disponible ($)": p50_cash.round(2),
        "Cash (Percentile 95) ($)": p95_cash.round(2),
        "Cash (Percentile 5) ($)": p5_cash.round(2)
    })
    
    # Enregistrement des données dans le workspace
    csv_path = "C:/Users/kevin/.gemini/antigravity/brain/97645884-0ef2-413c-8447-3f9b5dad3583/scratch/lark_stacking_results.csv"
    df_results.to_csv(csv_path, index=False)
    
    # 2. Création du graphique de Monte-Carlo
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    
    # Graphique 1 : Courbe de croissance du cash libre
    ax1.plot(months_range, p50_cash, color='#10b981', linewidth=2.5, label='Trajectoire Médiane (Optimisée V20)')
    ax1.fill_between(months_range, p5_cash, p95_cash, color='#10b981', alpha=0.15, label='Intervalle de Confiance (90%)')
    ax1.set_title('Simulation de Monte-Carlo : Croissance du Cash Libre (Profit Split)', fontsize=12, fontweight='bold', pad=15)
    ax1.set_ylabel('Cash Disponible ($)', fontweight='bold')
    ax1.grid(True, linestyle='--', alpha=0.5)
    ax1.legend(loc='upper left')
    
    # Graphique 2 : Évolution du Capital sous Gestion (Stacking)
    ax2.step(months_range, p50_cap, color='#3b82f6', linewidth=2.5, where='mid', label='Allocation Médiane (Limite : 300K$)')
    ax2.set_title('Évolution de l\'Allocation Capitalisée (Stacking de comptes)', fontsize=12, fontweight='bold', pad=15)
    ax2.set_xlabel('Mois de Déploiement', fontweight='bold')
    ax2.set_ylabel('Capital Financé ($)', fontweight='bold')
    ax2.set_ylim(0, 350000)
    ax2.grid(True, linestyle='--', alpha=0.5)
    ax2.legend(loc='lower right')
    
    plt.tight_layout()
    plot_path = "C:/Users/kevin/.gemini/antigravity/brain/97645884-0ef2-413c-8447-3f9b5dad3583/scratch/lark_stacking_simulation_plot.png"
    plt.savefig(plot_path, dpi=150)
    plt.close()
    
    print("[+] Rapport et graphiques générés avec succès.")
    return df_results

if __name__ == "__main__":
    print("[+] Initialisation de la simulation géométrique...")
    cash, capital = run_monte_carlo_simulation(num_paths=2000, months=12)
    df = generate_report_and_plots(cash, capital)
    print("\nTrajectoire V20-Fix sur 12 Mois (Percentile 50) :")
    print(df.to_string(index=False))
