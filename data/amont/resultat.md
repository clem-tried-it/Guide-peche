# Test des débits amont (Hub'Eau) — 10/11/2026 09:30 UTC

## Disponibilité des données

| Aval | Station amont | code | jours (QmnJ) | de | à | médiane m³/s | heures temps réel (31 j) |
|---|---|---|---|---|---|---|---|
| Chooz | Montherme | B700001002 | 692 | 2022-01-05 | 2026-11-09 | 524.4 | 24 |
| Chooz | Aiglemont | B560010101 | 985 | 2021-12-10 | 2026-11-09 | 217.6 | 0 |
| Chooz | Charleville | B540001001 | 0 | - | - | - | 0 |
| Chooz | Sedan | B502001001 | 1835 | 2021-11-01 | 2026-11-09 | 54.7 | 364 |
| Chooz | Stenay | B315002001 | 1835 | 2021-11-01 | 2026-11-09 | 28.9 | 716 |
| Landelies | Marpent | D019223001 | 1835 | 2021-11-01 | 2026-11-09 | 5.1 | 716 |
| Landelies | Maubeuge | D019801101 | 0 | - | - | - | 0 |
| Landelies | Berlaimont | D016221001 | 1835 | 2021-11-01 | 2026-11-09 | 38.8 | 0 |
| Landelies | HelpeMaj_Taisnieres | D015652001 | 1835 | 2021-11-01 | 2026-11-09 | 1.5 | 716 |
| Landelies | HelpeMin_Maroilles | D013702001 | 1835 | 2021-11-01 | 2026-11-09 | 1.7 | 486 |
| Landelies | Solre_Ferriere | D020601001 | 1835 | 2021-11-01 | 2026-11-09 | 0.6 | 680 |

## Backtest journalier

Même modèle statistique direct par horizon, avec et sans les variations de débit amont de la veille et du jour. « Montées » = jours où le débit a réellement pris plus de 20 % sur l'horizon. Biais négatif = hausse sous-estimée.

### Chooz — erreur médiane (%), test juin 2024 → aujourd'hui, pluie réelle connue pour toutes les variantes

| Variante | J+1 | J+2 | J+3 | J+5 | J+7 | J+1 montées | J+2 montées | J+3 montées | biais montées J+2 | jours test |
|---|---|---|---|---|---|---|---|---|---|---|
| sans amont | 3.7 | 5.8 | 7.0 | 9.5 | 12.6 | 15 (n=56) | 18 (n=100) | 20 (n=134) | -16 % | 893 |
| + Aiglemont | 2.3 | 4.2 | 7.1 | 10.7 | 13.2 | n/d | 9 (n=12) | 16 (n=18) | -4 % | 150 |
| + Sedan | 2.7 | 5.0 | 6.3 | 8.5 | 11.9 | 11 (n=56) | 14 (n=100) | 17 (n=134) | -8 % | 893 |
| + Stenay | 3.0 | 5.0 | 6.3 | 9.0 | 12.0 | 13 (n=56) | 16 (n=100) | 18 (n=134) | -10 % | 893 |
| + toutes | 1.7 | 4.4 | 7.2 | 10.6 | 12.9 | n/d | 8 (n=12) | 14 (n=18) | -0 % | 150 |

### Landelies — erreur médiane (%), test juin 2024 → aujourd'hui, pluie réelle connue pour toutes les variantes

| Variante | J+1 | J+2 | J+3 | J+5 | J+7 | J+1 montées | J+2 montées | J+3 montées | biais montées J+2 | jours test |
|---|---|---|---|---|---|---|---|---|---|---|
| sans amont | 10.0 | 12.4 | 13.7 | 16.3 | 19.2 | 22 (n=137) | 24 (n=186) | 25 (n=206) | -20 % | 831 |
| + Marpent | 10.1 | 12.2 | 13.2 | 15.9 | 19.5 | 21 (n=137) | 24 (n=186) | 25 (n=206) | -20 % | 831 |
| + Berlaimont | n/d | n/d | n/d | n/d | n/d | n/d | n/d | n/d | n/d | 0 |
| + HelpeMaj_Taisnieres | 9.6 | 12.7 | 13.7 | 15.8 | 18.9 | 21 (n=137) | 24 (n=186) | 24 (n=206) | -21 % | 831 |
| + HelpeMin_Maroilles | 9.6 | 11.9 | 13.2 | 15.1 | 19.2 | 23 (n=137) | 24 (n=186) | 25 (n=206) | -20 % | 831 |
| + Solre_Ferriere | 10.5 | 12.3 | 13.7 | 15.4 | 19.5 | 20 (n=137) | 22 (n=186) | 23 (n=206) | -19 % | 831 |
| + toutes | n/d | n/d | n/d | n/d | n/d | n/d | n/d | n/d | n/d | 0 |

## Avance horaire


### Chooz — avance horaire de l'amont (30 derniers jours)

| Station amont | heures de données | dernier relevé (UTC) | meilleur décalage (h) | corrélation | rapport médian aval/amont |
|---|---|---|---|---|---|
| Montherme | 24 | 2026-10-23 08:00:00+00:00 | n/d | n/d | n/d |
| Aiglemont | 0 | - | n/d | n/d | n/d |
| Charleville | 0 | - | n/d | n/d | n/d |
| Sedan | 364 | 10/11 09:00 | 1 | 0.47 | 1.39 |
| Stenay | 716 | 10/11 09:00 | 0 | 0.41 | 6.07 |
