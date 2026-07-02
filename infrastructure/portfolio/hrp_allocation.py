import numpy as np
import pandas as pd
import scipy.cluster.hierarchy as sch
import matplotlib.pyplot as plt

# ==============================================================================
# HIERARCHICAL RISK PARITY (HRP) IMPLEMENTATION
# Basé sur les travaux de Marcos López de Prado (Advances in Financial Machine Learning)
# ==============================================================================

def get_inverse_variance_portfolio(cov_matrix):
    """Calcule l'allocation par l'inverse de la variance (IVP)."""
    ivp = 1.0 / np.diag(cov_matrix)
    ivp /= ivp.sum()
    return ivp

def get_cluster_variance(cov_matrix, cluster_items):
    """Calcule la variance d'un cluster d'actifs."""
    cov_slice = cov_matrix.iloc[cluster_items, cluster_items]
    weights = get_inverse_variance_portfolio(cov_slice)
    variance = np.dot(weights.T, np.dot(cov_slice, weights))
    return variance

def get_quasi_diag(linkage_matrix):
    """
    Réorganise les éléments de la matrice pour regrouper les actifs 
    fortement corrélés le long de la diagonale.
    """
    linkage_matrix = linkage_matrix.astype(int)
    sort_ix = pd.Series([linkage_matrix[-1, 0], linkage_matrix[-1, 1]])
    num_items = linkage_matrix[-1, 3]
    
    while sort_ix.max() >= num_items:
        sort_ix.index = range(0, sort_ix.shape[0] * 2, 2)  # Créer de l'espace
        df0 = sort_ix[sort_ix >= num_items]
        i = df0.index
        j = df0.values - num_items
        sort_ix[i] = linkage_matrix[j, 0]
        df1 = pd.Series(linkage_matrix[j, 1], index=i + 1)
        sort_ix = pd.concat([sort_ix, df1])
        sort_ix = sort_ix.sort_index()
        sort_ix.index = range(sort_ix.shape[0])
        
    return sort_ix.tolist()

def get_recursive_bisection(cov_matrix, sort_ix):
    """Calcule les pondérations HRP via la bisection récursive de l'arbre."""
    weights = pd.Series(1.0, index=sort_ix)
    clusters = [sort_ix]
    
    while len(clusters) > 0:
        clusters = [c[i:j] for c in clusters for i, j in ((0, len(c) // 2), (len(c) // 2, len(c))) if len(c) > 1]
        for i in range(0, len(clusters), 2):
            cluster_left = clusters[i]
            cluster_right = clusters[i + 1]
            
            var_left = get_cluster_variance(cov_matrix, cluster_left)
            var_right = get_cluster_variance(cov_matrix, cluster_right)
            
            alpha = 1 - var_left / (var_left + var_right)
            
            weights[cluster_left] *= alpha
            weights[cluster_right] *= 1 - alpha
            
    return weights

def run_hrp_allocation(returns_df):
    """Exécute le pipeline HRP complet sur une série de rendements historiques."""
    # 1. Matrice de corrélation et de covariance
    corr_matrix = returns_df.corr()
    cov_matrix = returns_df.cov()
    
    # 2. Matrice de distance basée sur la corrélation (D_ij = sqrt(0.5 * (1 - rho_ij)))
    distance_matrix = np.sqrt((1 - corr_matrix).clip(0, 2) / 2)
    
    # 3. Tree Clustering (Regroupement hiérarchique via la méthode de Ward)
    import scipy.spatial.distance as ssd
    # Conversion de la matrice de distance en format condensé requis par scipy
    dist_array = ssd.squareform(distance_matrix)
    linkage_matrix = sch.linkage(dist_array, method='ward')
    
    # 4. Quasi-Diagonalisation
    sort_ix = get_quasi_diag(linkage_matrix)
    sort_ix = [returns_df.columns[i] for i in sort_ix]
    
    # Réorganisation de la matrice de covariance
    cov_matrix = cov_matrix.loc[sort_ix, sort_ix]
    
    # 5. Bisection Récursive
    hrp_weights = get_recursive_bisection(cov_matrix, range(cov_matrix.shape[0]))
    hrp_weights.index = cov_matrix.index
    
    return hrp_weights, linkage_matrix

# ==============================================================================
# SIMULATION DE DONNÉES ET TEST
# ==============================================================================
if __name__ == "__main__":
    print("[*] Génération de données historiques simulées (BTC, ETH, SOL, BNB)...")
    np.random.seed(42)
    
    # Création de rendements aléatoires corrélés
    days = 1000
    # On simule une forte corrélation entre BTC et ETH
    market_factor = np.random.normal(0, 0.02, days)
    btc = market_factor + np.random.normal(0, 0.01, days)
    eth = market_factor + np.random.normal(0, 0.015, days)
    sol = 0.5 * market_factor + np.random.normal(0, 0.03, days)
    bnb = 0.7 * market_factor + np.random.normal(0, 0.01, days)
    
    returns = pd.DataFrame({'BTC': btc, 'ETH': eth, 'SOL': sol, 'BNB': bnb})
    
    print("[*] Exécution de l'algorithme HRP (Tree Clustering + Quasi-Diag + Bisection)...")
    weights, Z = run_hrp_allocation(returns)
    
    print("\n[+] PONDÉRATIONS OPTIMALES DU PORTEFEUILLE (HRP) :")
    for asset, weight in weights.items():
        print(f"    - {asset} : {weight*100:.2f} %")
    
    # Affichage du Dendrogramme
    plt.figure(figsize=(10, 5))
    plt.title("Dendrogramme Hiérarchique des Actifs (HRP)")
    sch.dendrogram(Z, labels=returns.columns, leaf_rotation=90)
    plt.tight_layout()
    plt.savefig("C:/Users/kevin/.gemini/antigravity/brain/97645884-0ef2-413c-8447-3f9b5dad3583/scratch/hrp_dendrogram.png", dpi=150)
    plt.close()
    print("\n[+] Graphique du Dendrogramme généré : hrp_dendrogram.png")
