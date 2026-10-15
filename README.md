# Agent relances

**Un agent IA qui relance les clients impayés d'une PME au bon moment, sur le bon ton, sans jamais se tromper de montant, sous votre contrôle.**

Dans une PME, relancer les impayés est une corvée qu'on repousse : on oublie une facture, on relance trop tard, on écrit le même mail sec au meilleur client et au retardataire chronique. Cet agent prépare chaque matin les relances dues, adaptées au client et à l'historique, et un humain les valide en quelques secondes. Rien n'est jamais envoyé automatiquement.

C'est le troisième projet d'une suite d'agents pour PME : [agent-factures](https://github.com/micouaidan55/agent-factures) lit et vérifie les factures, [agent-emails](https://github.com/micouaidan55/agent-emails) trie la boîte de réception, et agent-relances suit l'argent jusqu'à l'encaissement.

## Ce que fait l'agent

1. **Lit les factures émises impayées** dans la base d'agent-factures, en lecture seule.
2. **Décide qui relancer, quand et comment** : trois paliers (rappel courtois à J+3, relance ferme à J+15, dernier rappel avant mise en demeure à J+30), regroupement des factures d'un même client, pas plus d'une relance par semaine, puis passage à un humain.
3. **Adapte le ton** au palier et au profil du client, calculé sur son historique de paiement : bon payeur, occasionnel, retardataire chronique.
4. **Fait rédiger la relance par Claude**, puis **vérifie chaque fait par le code** : numéros, montants, échéances, total, mention légale des 40 € pour un professionnel au dernier palier. Un montant faux ou inventé est refusé ; après deux échecs, un modèle de texte de secours prend le relais.
5. **Attend votre validation** : vous relisez, modifiez, validez (le brouillon `.eml` est déposé), reportez ou passez la main à un humain.

## Évaluation

30 scénarios générés couvrent chaque combinaison palier × profil, les particuliers (sans mention des 40 €), les regroupements de 2 et 3 factures, le délai minimal, les fiches manquantes, le passage à un humain et une fiche client qui tente de donner des ordres au modèle.

On mesure : l'exactitude des règles, les faits exacts selon le vérificateur (dès le premier essai et après vérification), le **ton reconnu à l'aveugle** par un juge qui ne connaît ni le palier ni le profil, la note du juge (l'adéquation du ton est mesurée par le ton deviné à l'aveugle), le recours au modèle de secours et le coût par relance.

Les résultats chiffrés seront publiés ici après le premier run d'évaluation. Pour reproduire (payant, plafonné) : `uv run python -m evals.run --max-cost 1`.

## Démarrage rapide

```bash
git clone https://github.com/micouaidan55/agent-relances.git && cd agent-relances
cp .env.example .env        # puis renseigner ANTHROPIC_API_KEY (facultatif : sans clé, le modèle de secours rédige)
uv sync
uv run python -m demo.seed  # base de démonstration ; TODAY=2026-10-15 est déjà dans .env.example ; à vider pour de vraies factures
uv run streamlit run app/main.py
```

La démonstration montre les trois paliers, un regroupement, un particulier, une fiche manquante, un passage à un humain et une facture encaissée après relance. Avec une clé d'API, préparer les relances du jour coûte de l'ordre du centime par relance (à confirmer par l'évaluation).

Pour relancer les vraies factures d'agent-factures, pointez `AGENT_FACTURES_DB` vers sa base (par exemple `../agent-factures/data/factures.db`) et videz `TODAY`.

## Architecture

```
base agent-factures (lecture seule) ─► factures impayées + historique de paiement
fiches clients ─► profil ─► paliers, regroupement, délai minimal, ton cible, mention légale   (code)
                                          │
                              rédaction par Claude ─► vérification des faits ─► secours si échec
                                          │
                     validation humaine : déposer (.eml) · modifier · reporter · passer à un humain
```

| Module | Rôle |
|---|---|
| `agent_relances.rules` | Règles de recouvrement : profil, paliers, regroupement, ton, mention légale (fonctions pures) |
| `agent_relances.drafting` | Rédaction par le modèle, vérification des faits, modèle de secours |
| `agent_relances.sources` | Lecture seule d'agent-factures |
| `agent_relances.storage` | SQLite : fiches clients, historique des relances, journal |
| `app/` | Interface Streamlit : relances du jour, clients, tableau de bord, journal |
| `evals/` | 30 scénarios, métriques, juge à l'aveugle |

### Le code décide, le modèle rédige, le code vérifie

Quand relancer, à quel palier et sur quel ton relève de règles métier : elles sont écrites en code, testées et identiques pour chaque client. Le modèle ne fait que ce qu'il fait mieux qu'un modèle de texte : écrire une relance naturelle, adaptée au client. Et comme un montant faux dans une relance, c'est un client fâché, le code relit chaque brouillon avant de vous le montrer : il refuse tout montant ou toute date qui ne figure pas dans les factures.

### Garde-fous

- **Lecture seule** sur la base d'agent-factures, qui reste la seule source de vérité sur les paiements.
- **Aucun envoi automatique** : la validation dépose un brouillon `.eml`.
- **Vérification des faits** avant affichage et à nouveau au dépôt, y compris après une modification humaine.
- **Pas de mise en demeure automatique** : après le dernier rappel, la facture passe à un humain.
- **Modèle de secours** : une panne d'API ou une sortie fausse ne bloque jamais une relance.

## Grille du juge

Le juge (Claude Sonnet) lit chaque relance sans connaître le contexte, devine son ton (léger, neutre, ferme) et la note de 1 à 5 sur la clarté, la politesse et la précision de la demande. La grille complète se trouve dans `evals/judge.py`.

## Tests

```bash
uv run pytest
```

Les tests utilisent un faux client et ne consomment aucun crédit d'API.

## Limites connues et feuille de route

- Les réponses des clients (promesse de paiement, contestation) ne sont pas encore lues : c'est le lien naturel avec agent-emails.
- Pas de priorisation de trésorerie ni de score de risque, pas de pénalités de retard calculées.
- Scénarios synthétiques : la mesure sur de vraies relances reste à faire.
