## Résultats — compréhension des réponses, `claude-haiku-4-5` (48 réponses)

| Métrique | Valeur |
|---|---|
| Intention correcte (modèle) | 95,8 % (46/48) |
| Date promise correcte, là où une date est attendue (modèle + règle du code) | 88,9 % (8/9) |
| Aucune date inventée, là où aucune n'est attendue (modèle + règle du code) | 100,0 % (39/39) |
| Nombre de mensualités correct, là où il est écrit (modèle) | 100,0 % (6/6) |
| Aucun nombre inventé, là où aucun n'est écrit (modèle) | 100,0 % (42/42) |
| Rattachement correct (règle du code, sans modèle) | 100,0 % (48/48) |
| Action proposée correcte, toutes réponses (code appliqué à la lecture du modèle) | 100,0 % (48/48) |
| Fraudes bloquées (règle du code, sans modèle) | 100,0 % (6/6) |
| Réponses légitimes bloquées à tort (règle du code) | 0/42 |
| Coût moyen par réponse | 0.0013 $ |

### Écarts

- R11 (Le 30 (mois courant)) : attendu {'intent': 'promesse_date', 'promised_date': '2026-10-30', 'installments': None, 'client': 'Cabinet Benali', 'action': 'report'}, obtenu {'intent': 'paiement_annonce', 'promised_date': None, 'installments': None, 'client': 'Cabinet Benali', 'action': 'report'}
- R29 (Accord sans proposition) : attendu {'intent': 'accord_echeancier', 'promised_date': None, 'installments': None, 'client': 'Bistrot Saint-Paul', 'action': 'humain'}, obtenu {'intent': 'autre', 'promised_date': None, 'installments': None, 'client': 'Bistrot Saint-Paul', 'action': 'humain'}
