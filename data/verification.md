# Vérification des estimations émises avec les alertes

Mis à jour le 17/11/2026 00:54. 5 alerte(s) journalisée(s), 1 estimation(s) déjà comparée(s) au débit réel.

Erreur = écart entre le débit estimé et le débit moyen observé le jour visé. « Débit constant » = ce qu'aurait donné l'hypothèse « le débit ne bouge pas ». Le modèle n'est utile que s'il fait mieux.

## Meuse (Chooz)

| Horizon | Cas | Erreur médiane modèle | Erreur médiane « débit constant » | Biais médian | Dans la fourchette |
|---|---|---|---|---|---|
| J+1 | 1 | 24 % | 41 % | -24 % | 0/1 |

## Détail par alerte

| Alerte | Station | Type | Débit au départ | J+1 | J+3 | J+7 |
|---|---|---|---|---|---|---|
| 2026-11-15 23:00 | Chooz | décrue | 24 m³/s | estimé 31, réel 40 (-24 %) | estimé 82, à venir | estimé 143, à venir |
| 2026-11-16 00:00 | Chooz | décrue | 27 m³/s | estimé 45, à venir | estimé 111, à venir | estimé 130, à venir |
| 2026-11-16 02:00 | Chooz | décrue | 26 m³/s | estimé 45, à venir | estimé 123, à venir | estimé 155, à venir |
| 2026-11-16 07:00 | Chooz | crue | 38 m³/s | estimé 60, à venir | estimé 138, à venir | estimé 174, à venir |
| 2026-11-16 19:00 | Chooz | décrue | 40 m³/s | estimé 66, à venir | estimé 129, à venir | estimé 141, à venir |

Limite : ces estimations sont émises au moment des alertes, donc dans les situations les plus difficiles (rivière en mouvement). Les erreurs seront plus fortes que celles du backtest, qui couvre tous les jours.
