#!/usr/bin/env python3
"""
cTrader Open API Authentication Utility - OAuth2 Flow
Developed for Predator Institutional Architecture V20

This script handles the OAuth2 authorization code grant flow to acquire a long-lived Access Token 
for your Lark Funding cTrader account. 

Prerequisites:
1. Register an application on connect.spotware.com to get ClientID and ClientSecret.
2. Ensure your Redirect URI is set (e.g., http://localhost:8080/ or https://localhost).
"""

import sys
import json
import urllib.request
import urllib.parse

def generate_auth_url(client_id: str, redirect_uri: str) -> str:
    """Generates the secure cTrader authorization URL."""
    base_url = "https://openapi.ctrader.com/apps/auth"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "trading", # Modifié : "trading" au lieu de "accounts" pour pouvoir passer des ordres
    }
    return f"{base_url}?{urllib.parse.urlencode(params)}"

def exchange_code_for_tokens(client_id: str, client_secret: str, redirect_uri: str, auth_code: str) -> dict:
    """Exchanges the temporary authorization code for permanent access and refresh tokens."""
    url = "https://openapi.ctrader.com/apps/token"
    params = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri
    }
    
    # OpenAPI token endpoint expects query parameters or urlencoded form
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    
    try:
        print("[+] Envoi de la requête d'échange de jeton à Spotware...")
        with urllib.request.urlopen(req) as response:
            if response.status == 200:
                return json.loads(response.read().decode("utf-8"))
            else:
                raise Exception(f"Erreur HTTP {response.status}: {response.read().decode('utf-8')}")
    except urllib.error.HTTPError as e:
        error_msg = e.read().decode("utf-8")
        raise Exception(f"HTTPError: {e.code} - {error_msg}")
    except Exception as e:
        raise Exception(f"Erreur lors de la requête: {str(e)}")

def main():
    # Utilisations des clés codées en dur pour simplifier l'exécution via l'Agent
    client_id = "31781_hUWypKoPcwnQTrOigKcHF20zhjPZP8MkBrAYu0OTnDvTCl6fGm"
    client_secret = "iK9dH8K5w19AodjRfLUP6EPZ8aAqmRBN200MpekCRdf857uvmM"
    redirect_uri = "https://localhost"

    # Vérifie si le code d'authentification a été passé en argument
    if len(sys.argv) > 1:
        auth_code = sys.argv[1]
        print(f"[*] Échange du code d'autorisation {auth_code}...")
        try:
            tokens = exchange_code_for_tokens(client_id, client_secret, redirect_uri, auth_code)
            
            token_filename = "C:/Users/kevin/.gemini/antigravity/brain/97645884-0ef2-413c-8447-3f9b5dad3583/scratch/ctrader_tokens.json"
            with open(token_filename, "w") as f:
                json.dump(tokens, f, indent=4)
                
            print(f"\n[+] [SUCCÈS] Authentification validée !")
            print(f"[+] Access Token sauvegardé dans : '{token_filename}'")
        except Exception as e:
            print(f"[-] Erreur critique : {str(e)}")
    else:
        # Génération de l'URL d'autorisation
        auth_url = generate_auth_url(client_id, redirect_uri)
        print("\n" + "=" * 80)
        print(" ÉTAPE 1 : AUTORISATION DU COMPTE")
        print("=" * 80)
        print("Veuillez copier et coller l'URL ci-dessous dans votre navigateur Web :")
        print(f"\n{auth_url}\n")
        print("Instructions :")
        print("1. Connectez-vous à votre compte cTrader (Spotware ID).")
        print("2. Autorisez l'accès à l'application.")
        print("3. Vous serez redirigé vers https://localhost?code=XYZ")
        print("4. Copiez le code XYZ et donnez-le moi dans le chat.")

if __name__ == "__main__":
    main()
