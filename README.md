# Guide pêche – débits Sambre-Meuse

Relevé horaire des débits de la Sambre et de la Meuse (SPW Hydrométrie), journal en CSV et alertes
crue / décrue en notification ntfy avec graphiques et estimation du débit à 14 jours.

- `debits.py` : le programme (relevé, alertes, graphiques, modèles d'estimation).
- `config/seuils.json` : règles d'alerte, modifiables directement ici.
- `data/releves.csv` (horaire), `data/historique.csv` (journalier depuis nov. 2021), `data/alertes.csv`.
- `state.json` : mémoire des dernières alertes (ne pas modifier).

Lancement : chaque heure par GitHub Actions. À la main : onglet *Actions* > *Guide pêche – débits Sambre-Meuse* >
*Run workflow* (`run`, `test` ou `bootstrap`).

Le canal ntfy est stocké dans le secret `NTFY_TOPIC` (Settings > Secrets and variables > Actions).

Données : SPW Hydrométrie (KiWIS) ; pluie, évaporation et prévisions : Open-Meteo (usage non commercial).
