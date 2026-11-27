# Vérification des estimations émises avec les alertes

Mis à jour le 28/11/2026 00:10. 8 alerte(s) journalisée(s), 75 estimation(s) déjà comparée(s) au débit réel.

Erreur = écart entre le débit estimé et le débit moyen observé le jour visé. « Débit constant » = ce qu'aurait donné l'hypothèse « le débit ne bouge pas ». Le modèle n'est utile que s'il fait mieux.

## Meuse (Chooz)

| Horizon | Cas | Erreur médiane modèle | Erreur médiane « débit constant » | Biais médian | Dans la fourchette |
|---|---|---|---|---|---|
| J+1 | 7 | 33 % | 59 % | -21 % | 2/6 |
| J+2 à J+3 | 14 | 26 % | 77 % | -23 % | 7/12 |
| J+4 à J+7 | 25 | 19 % | 82 % | -16 % | 14/24 |
| J+8 à J+14 | 24 | 53 % | 61 % | +53 % | 5/24 |

## Sambre (Landelies)

| Horizon | Cas | Erreur médiane modèle | Erreur médiane « débit constant » | Biais médian | Dans la fourchette |
|---|---|---|---|---|---|
| J+1 | 1 | 1 % | 42 % | -1 % | 0/1 |
| J+2 à J+3 | 2 | 57 % | 231 % | +57 % | 0/2 |
| J+4 à J+7 | 2 | 110 % | 522 % | +110 % | 0/2 |

## Détail par alerte

| Alerte | Station | Type | Débit au départ | J+1 | J+3 | J+7 |
|---|---|---|---|---|---|---|
| 2026-11-15 23:00 | Chooz | décrue | 24 m³/s | estimé 31, réel 39 (-21 %) | estimé 82, réel 131 (-37 %) | estimé 143, réel 191 (-25 %) |
| 2026-11-16 00:00 | Chooz | décrue | 27 m³/s | estimé 45, réel 67 (-33 %) | estimé 111, réel 150 (-26 %) | estimé 130, réel 141 (-8 %) |
| 2026-11-16 02:00 | Chooz | décrue | 26 m³/s | estimé 45, réel 67 (-33 %) | estimé 123, réel 150 (-18 %) | estimé 155, réel 141 (+10 %) |
| 2026-11-16 07:00 | Chooz | crue | 38 m³/s | estimé 60, réel 67 (-11 %) | estimé 138, réel 150 (-8 %) | estimé 174, réel 141 (+23 %) |
| 2026-11-16 19:00 | Chooz | décrue | 40 m³/s | estimé 66, réel 67 (-2 %) | estimé 129, réel 150 (-14 %) | estimé 141, réel 141 (+0 %) |
| 2026-11-17 12:00 | Chooz | crue | 52 m³/s | estimé 84, réel 131 (-36 %) | estimé 128, réel 161 (-21 %) | estimé 111, réel 101 (+11 %) |
| 2026-11-22 18:00 | Landelies | décrue | 50 m³/s | estimé 35, réel 35 (-1 %) | estimé 22, réel 12 (+81 %) | estimé 13, à venir |
| 2026-11-23 05:00 | Chooz | décrue | 168 m³/s | estimé 154, réel 101 (+53 %) | estimé 145, réel 75 (+93 %) | estimé 132, à venir |

Limite : ces estimations sont émises au moment des alertes, donc dans les situations les plus difficiles (rivière en mouvement). Les erreurs seront plus fortes que celles du backtest, qui couvre tous les jours.
