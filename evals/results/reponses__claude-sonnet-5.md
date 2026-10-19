## Résultats — compréhension des réponses, `claude-sonnet-5` (48 réponses)

| Métrique | Valeur |
|---|---|
| Intention correcte (modèle) | 100,0 % (48/48) |
| Date promise correcte, là où une date est attendue (modèle + règle du code) | 100,0 % (9/9) |
| Aucune date inventée, là où aucune n'est attendue (modèle + règle du code) | 100,0 % (39/39) |
| Nombre de mensualités correct, là où il est écrit (modèle) | 100,0 % (6/6) |
| Aucun nombre inventé, là où aucun n'est écrit (modèle) | 97,6 % (41/42) |
| Rattachement correct (règle du code, sans modèle) | 100,0 % (48/48) |
| Action proposée correcte, toutes réponses (code appliqué à la lecture du modèle) | 100,0 % (48/48) |
| Fraudes bloquées (règle du code, sans modèle) | 100,0 % (6/6) |
| Réponses légitimes bloquées à tort (règle du code) | 0/42 |
| Coût moyen par réponse | 0.0036 $ |

### Écarts

- R25 (Accord trois mensualités) : attendu {'intent': 'accord_echeancier', 'promised_date': None, 'installments': None, 'client': 'Cabinet Benali', 'action': 'activation_echeancier'}, obtenu {'intent': 'accord_echeancier', 'promised_date': None, 'installments': 3, 'client': 'Cabinet Benali', 'action': 'activation_echeancier'}
