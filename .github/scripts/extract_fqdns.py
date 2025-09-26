#!/usr/bin/env python3
"""
Script pour extraire les valeurs FQDN des fichiers YAML/YML dans le dossier system/
et générer un fichier gatus-config.yml compatible avec Gatus
"""

import os
import yaml
import glob
import re
from pathlib import Path
from datetime import datetime


def find_fqdn_in_yaml(file_path):
    """
    Extrait les valeurs FQDN d'un fichier YAML (supporte les multi-documents)
    """
    # Ignorer les fichiers templates Helm qui causent des erreurs de parsing
    if "/templates/" in str(file_path) or "\\templates\\" in str(file_path):
        return []

    fqdns = []
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            # Utiliser yaml.safe_load_all pour gérer les multi-documents
            documents = yaml.safe_load_all(file)
            for doc in documents:
                if doc:  # Ignorer les documents vides
                    fqdns.extend(extract_fqdn_recursive(doc, str(file_path)))
    except Exception as e:
        print(f"Erreur lors de la lecture de {file_path}: {e}")

    return fqdns


def is_valid_fqdn(fqdn):
    """
    Vérifie si un FQDN est valide pour la surveillance (pas un placeholder ou exemple)
    """
    invalid_patterns = [
        "example.com",
        "example.local",
        "chart-example.local",
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        ".local",
        "example.org",
        "test.com",
        "httpbin.org",  # Found in ArgoCD config as example
        "quay.io",  # Container registry, not a service to monitor
        "github.com",  # Git repositories, not services to monitor
        "kubernetes.io",  # Documentation URLs, not services to monitor
        "argoproj.io",  # ArgoCD project URLs, not services to monitor
        "hashicorp.com",  # Documentation URLs, not services to monitor
        "redhat.io",  # API references, not services to monitor
        "microsoftonline.com",  # OAuth endpoints, not direct services to monitor
    ]

    # Vérifier si le FQDN contient des patterns invalides
    fqdn_lower = fqdn.lower()
    for pattern in invalid_patterns:
        if pattern in fqdn_lower:
            return False

    # Vérifier si c'est un vrai domaine (contient au moins un point et pas de variables Helm)
    if "." not in fqdn or "{" in fqdn or "}" in fqdn or fqdn.startswith("{{"):
        return False

    return True


def extract_fqdn_recursive(obj, debug_path=""):
    """
    Recherche récursivement les clés contenant des FQDNs dans un objet YAML
    Cherche: 'fqdn', 'host', 'hosts', 'dnsNames', 'commonName', 'domain', 'issuer'
    """
    fqdns = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            current_path = f"{debug_path}.{key}" if debug_path else key

            # Clés qui contiennent directement des FQDNs
            if key in ["fqdn", "host", "commonName", "domain"] and isinstance(
                value, str
            ):
                # Filtrer seulement les domaines qui ressemblent à des FQDNs (contiennent un point)
                if (
                    "." in value
                    and not value.startswith("http")
                    and is_valid_fqdn(value)
                ):
                    fqdns.append(value)
                    print(f"  Found FQDN '{value}' at {current_path}")
            # Clés qui peuvent contenir des URLs avec des FQDNs (comme issuer)
            elif key in ["issuer", "url", "endpoint"] and isinstance(value, str):
                print(f"  Checking URL field '{key}' with value: {value}")
                # Extraire le FQDN des URLs
                if value.startswith("http"):
                    # Extraire le domaine de l'URL
                    url_match = re.search(r"https?://([^/]+)", value)
                    if url_match:
                        domain = url_match.group(1)
                        print(f"  Extracted domain '{domain}' from URL")
                        if is_valid_fqdn(domain):
                            fqdns.append(domain)
                            print(f"  Added FQDN '{domain}' from URL at {current_path}")
                        else:
                            print(f"  Domain '{domain}' failed validation")
                elif "." in value and is_valid_fqdn(value):
                    fqdns.append(value)
                    print(f"  Found direct FQDN '{value}' at {current_path}")
            # Clés qui contiennent des listes de FQDNs
            elif key in ["hosts", "dnsNames"] and isinstance(value, list):
                for item in value:
                    if (
                        isinstance(item, str)
                        and "." in item
                        and not item.startswith("http")
                        and is_valid_fqdn(item)
                    ):
                        fqdns.append(item)
                        print(f"  Found FQDN '{item}' in list at {current_path}")
            # Special handling for embedded YAML strings (like dex.config)
            elif isinstance(value, str) and (
                "\n" in value and (":" in value or "issuer:" in value)
            ):
                print(f"  Checking embedded YAML in field '{key}'")
                try:
                    # Try to parse as YAML
                    embedded_yaml = yaml.safe_load(value)
                    if embedded_yaml:
                        fqdns.extend(
                            extract_fqdn_recursive(
                                embedded_yaml, f"{current_path}[embedded]"
                            )
                        )
                except yaml.YAMLError:
                    # Not valid YAML, continue
                    pass
            else:
                # Continuer la recherche récursive
                fqdns.extend(extract_fqdn_recursive(value, current_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            fqdns.extend(extract_fqdn_recursive(item, f"{debug_path}[{i}]"))

    return fqdns


def create_simple_endpoint(fqdn, app_name, source_file):
    """
    Crée un endpoint simple pour un FQDN donné
    """
    # Créer un nom unique basé sur le FQDN complet
    if "staging" in fqdn:
        endpoint_name = f"{app_name}-staging"
    else:
        # Utiliser le premier sous-domaine comme identifiant
        subdomain = fqdn.split(".")[0]
        endpoint_name = f"{app_name}-{subdomain}"

    endpoint = {
        "name": endpoint_name,
        "url": f"https://{fqdn}",
        "interval": "5m",
        "conditions": ["[STATUS] == 200", "[RESPONSE_TIME] < 3000"],
    }

    return endpoint


def main():
    """
    Fonction principale
    """
    # Chemin vers le dossier system
    system_dir = Path("system")

    if not system_dir.exists():
        print("Le dossier 'system' n'existe pas dans le répertoire courant")
        return

    all_fqdns = []

    # Parcourir récursivement tous les fichiers YAML/YML dans system/
    yaml_patterns = ["**/*.yaml", "**/*.yml"]

    for pattern in yaml_patterns:
        for yaml_file in system_dir.glob(pattern):
            print(f"Analyse du fichier: {yaml_file}")
            fqdns = find_fqdn_in_yaml(yaml_file)

            for fqdn in fqdns:
                # Ajouter des métadonnées sur l'origine du FQDN
                endpoint_info = {
                    "fqdn": fqdn,
                    "source_file": str(yaml_file),
                    "app_name": (
                        yaml_file.parts[1] if len(yaml_file.parts) > 1 else "unknown"
                    ),
                }
                all_fqdns.append(endpoint_info)

    # Supprimer les doublons en gardant la première occurrence
    unique_fqdns = []
    seen_fqdns = set()

    for endpoint in all_fqdns:
        if endpoint["fqdn"] not in seen_fqdns:
            unique_fqdns.append(endpoint)
            seen_fqdns.add(endpoint["fqdn"])

    # Trier par nom d'application puis par FQDN
    unique_fqdns.sort(key=lambda x: (x["app_name"], x["fqdn"]))

    # Créer la liste des endpoints
    endpoints_list = []
    for endpoint_info in unique_fqdns:
        endpoint = create_simple_endpoint(
            endpoint_info["fqdn"],
            endpoint_info["app_name"],
            endpoint_info["source_file"],
        )
        endpoints_list.append(endpoint)

    # Structure simple avec juste les endpoints
    endpoints_config = {
        "# Configuration générée automatiquement": f"Généré le {datetime.now().isoformat()}",
        "endpoints": endpoints_list,
    }

    # Écrire le fichier gatus-endpoints.yml
    with open("gatus-endpoints.yml", "w", encoding="utf-8") as f:
        yaml.dump(
            endpoints_config,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    print(
        f"Fichier gatus-endpoints.yml généré avec {len(unique_fqdns)} endpoints uniques"
    )

    # Afficher un résumé
    print("\nEndpoints trouvés:")
    for endpoint in unique_fqdns:
        print(
            f"  - {endpoint['fqdn']} (app: {endpoint['app_name']}, source: {Path(endpoint['source_file']).name})"
        )

    print(f"\nFichier généré:")
    print(f"  - gatus-endpoints.yml (endpoints pour Gatus)")


if __name__ == "__main__":
    main()
