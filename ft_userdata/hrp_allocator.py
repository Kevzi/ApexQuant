import pandas as pd
import numpy as np
import scipy.cluster.hierarchy as sch
from scipy.spatial.distance import squareform
import matplotlib.pyplot as plt
import argparse

def getIVP(cov, **kargs):
    # Inverse-Variance Portfolio
    ivp = 1. / np.diag(cov)
    ivp /= ivp.sum()
    return ivp

def getClusterVar(cov, cItems):
    # Calculate cluster variance
    cov_ = cov.loc[cItems, cItems]
    w_ = getIVP(cov_).reshape(-1, 1)
    cVar = np.dot(np.dot(w_.T, cov_), w_)[0, 0]
    return cVar

def getQuasiDiag(link):
    # Sort clustered items by distance
    link = link.astype(int)
    sortIx = pd.Series([link[-1, 0], link[-1, 1]])
    numItems = link[-1, 3]
    while sortIx.max() >= numItems:
        sortIx.index = range(0, sortIx.shape[0] * 2, 2)
        df0 = sortIx[sortIx >= numItems]
        i = df0.index
        j = df0.values - numItems
        sortIx[i] = link[j, 0]
        df0 = pd.Series(link[j, 1], index=i + 1)
        sortIx = pd.concat([sortIx, df0])
        sortIx = sortIx.sort_index()
        sortIx.index = range(len(sortIx))
    return sortIx.tolist()

def getRecBipart(cov, sortIx):
    # Recursive bisection
    w = pd.Series(1.0, index=sortIx)
    cItems = [sortIx]
    while len(cItems) > 0:
        cItems = [i[j:k] for i in cItems for j, k in ((0, len(i) // 2), (len(i) // 2, len(i))) if len(i) > 1]
        for i in range(0, len(cItems), 2):
            cItems0 = cItems[i]
            cItems1 = cItems[i + 1]
            cVar0 = getClusterVar(cov, cItems0)
            cVar1 = getClusterVar(cov, cItems1)
            alpha = 1 - cVar0 / (cVar0 + cVar1)
            w[cItems0] *= alpha
            w[cItems1] *= 1 - alpha
    return w

def correlDist(corr):
    dist = ((1 - corr) / 2.)**.5
    return dist

def generate_dummy_data():
    np.random.seed(42)
    dates = pd.date_range('2025-01-01', periods=1000)
    returns = pd.DataFrame(index=dates)
    
    # Generate 5 strategies with some correlations
    # A market factor
    market = np.random.normal(0.0005, 0.015, 1000)
    
    returns['Trend_Following_5m'] = market * 1.5 + np.random.normal(0, 0.02, 1000)
    returns['V14_PPO_Futures'] = market * 0.8 + np.random.normal(0.001, 0.01, 1000)
    returns['V14_PPO_Futures_Beta'] = returns['V14_PPO_Futures'] * 0.9 + np.random.normal(0, 0.002, 1000)
    returns['Mean_Reversion_15m'] = -market * 0.5 + np.random.normal(0.0005, 0.012, 1000)
    returns['Options_Writing_Theta'] = np.random.normal(0.001, 0.005, 1000)
    
    return returns

def main():
    parser = argparse.ArgumentParser(description='HRP Portfolio Allocator')
    parser.add_argument('--input', type=str, help='Path to CSV file with strategy returns')
    parser.add_argument('--output', type=str, default='hrp_allocations.csv', help='Output CSV file for weights')
    args = parser.parse_args()

    if args.input:
        df = pd.read_csv(args.input, index_col=0, parse_dates=True)
    else:
        print("Aucun fichier d'entrée fourni, génération de données de simulation...")
        df = generate_dummy_data()

    # Calculate covariance and correlation
    cov = df.cov()
    corr = df.corr()

    # Calculate distance matrix
    dist = correlDist(corr)
    
    # Hierarchical clustering using single linkage
    dist_array = squareform(dist.values, checks=False)
    link = sch.linkage(dist_array, 'single')

    # Quasi-diagonalization
    sortIx = getQuasiDiag(link)
    sortIx = corr.index[sortIx].tolist()
    df0 = corr.loc[sortIx, sortIx]

    # Recursive Bisection
    hrp_weights = getRecBipart(cov, sortIx)
    hrp_weights.index = sortIx
    
    # Calculate IVP weights for comparison
    ivp_weights = pd.Series(getIVP(cov), index=cov.index)

    # Calculate Annualized Volatility
    volatility = df.std() * np.sqrt(365)
    
    # Display results
    results = pd.DataFrame({
        'Poids HRP': hrp_weights * 100,
        'Poids IVP': ivp_weights * 100,
        'Vol. An.': volatility * 100
    }).loc[sortIx]
    
    print("\n=====================================================================")
    print("Stratégie                      | Poids HRP   | Poids IVP   | Vol. An.")
    print("---------------------------------------------------------------------")
    for idx, row in results.iterrows():
        print(f"{idx:30} | {row['Poids HRP']:9.2f}% | {row['Poids IVP']:9.2f}% | {row['Vol. An.']:9.2f}%")
    print("=====================================================================")

    # Save outputs
    results.to_csv(args.output)
    print(f"\nPoids optimaux sauvegardés dans {args.output}")

    # Plot Dendrogram
    plt.figure(figsize=(10, 6))
    sch.dendrogram(link, labels=corr.columns, leaf_rotation=45)
    plt.title('HRP Dendrogram (Hierarchical Clustering)')
    plt.tight_layout()
    plt.savefig('hrp_dendrogram.png')
    
    # Plot Correlation Matrix
    plt.figure(figsize=(8, 6))
    plt.pcolor(df0, cmap='RdYlGn')
    plt.colorbar()
    plt.yticks(np.arange(0.5, len(df0.index), 1), df0.index)
    plt.xticks(np.arange(0.5, len(df0.columns), 1), df0.columns, rotation=45)
    plt.title('Quasi-Diagonalized Correlation Matrix')
    plt.tight_layout()
    plt.savefig('hrp_correlation_matrix.png')
    
    print("Graphiques sauvegardés : hrp_dendrogram.png et hrp_correlation_matrix.png")

if __name__ == '__main__':
    main()
