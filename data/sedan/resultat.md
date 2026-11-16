# Test Sedan dans l'ensemble GR4J + statistique — Chooz (16/11/2026 06:47 UTC)

Rejeu jour par jour du 01/06/2024 au 31/10/2026 (875 jours). Pluie : prévisions réellement émises (Open-Meteo previous runs, modèle ecmwf_ifs025) de J+0 à J+7, pluie observée de J+8 à J+14 (identique pour toutes les variantes). Modèle statistique recalé chaque jour sur l'historique disponible ce jour-là, comme en production. Erreur = écart médian au débit moyen mesuré.

« Biais montées » : jours où le débit a réellement pris plus de 20 % ; négatif = hausse sous-estimée.

| Variante | J+1 | J+2 | J+3 | J+5 | J+7 | J+14 | biais montées J+2 | biais montées J+3 |
|---|---|---|---|---|---|---|---|---|
| Débit constant | 5.8 % | 9.6 % | 13.4 % | 19.1 % | 23.8 % | 30.6 % | -28 % (n=98) | -29 % (n=131) |
| GR4J seul | 4.5 % | 6.1 % | 7.3 % | 9.9 % | 11.9 % | 12.5 % | -16 % (n=98) | -15 % (n=131) |
| Statistique sans Sedan | 3.7 % | 5.5 % | 6.7 % | 8.8 % | 12.0 % | 15.7 % | -16 % (n=98) | -15 % (n=131) |
| Ensemble sans Sedan | 3.9 % | 5.6 % | 6.6 % | 8.8 % | 10.9 % | 11.8 % | -15 % (n=98) | -14 % (n=131) |
| Statistique Sedan, futur inconnu = 0 | 2.9 % | 5.4 % | 7.4 % | 10.8 % | 14.2 % | 20.6 % | -16 % (n=98) | -18 % (n=131) |
| Ensemble Sedan, futur inconnu = 0 | 3.5 % | 5.4 % | 7.1 % | 9.7 % | 12.0 % | 14.6 % | -16 % (n=98) | -16 % (n=131) |
| Statistique Sedan, futur = variation de Chooz | 2.9 % | 5.0 % | 7.2 % | 11.2 % | 15.4 % | 20.6 % | -7 % (n=98) | -5 % (n=131) |
| Ensemble Sedan, futur = variation de Chooz | 3.5 % | 4.6 % | 5.9 % | 8.7 % | 11.2 % | 12.9 % | -10 % (n=98) | -11 % (n=131) |

## Verdict

Règle fixée avant le test : intégrer seulement si aucun horizon ne se dégrade (tolérance 0,2 point) et si l'erreur baisse à J+1 à J+3.

- Sedan, futur inconnu = 0 : J+1 -0.4 pt, J+2 -0.2 pt, J+3 +0.5 pt, J+5 +0.9 pt, J+7 +1.1 pt, J+14 +2.8 pt → non conforme
- Sedan, futur = variation de Chooz : J+1 -0.4 pt, J+2 -1.0 pt, J+3 -0.7 pt, J+5 -0.1 pt, J+7 +0.3 pt, J+14 +1.0 pt → non conforme

**Décision : ne pas intégrer.**

## Journal

- Sedan : Hub'Eau à jour jusqu'au 15/11/2026
- Prévisions archivées : modèle ecmwf_ifs025, couverture J+0:100%, J+1:100%, J+2:100%, J+3:100%, J+4:100%, J+5:99%, J+6:100%, J+7:99%
- Jours testés : 875 (écartés faute de prévision : 8)
- GR4J rejoué
- Variante rejouée : sans Sedan
- Variante rejouée : Sedan, futur inconnu = 0
- Variante rejouée : Sedan, futur = variation de Chooz
