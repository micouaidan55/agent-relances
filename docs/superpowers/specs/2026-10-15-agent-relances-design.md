# Agent de relance des clients impayés pour PME — Design

**Date :** 2026-10-15
**Statut :** validé en brainstorming, en attente de relecture
**Projets liés :** [agent-factures](https://github.com/micouaidan55/agent-factures) (projet 1), [agent-emails](https://github.com/micouaidan55/agent-emails) (projet 2)

## 1. Objectif

Troisième projet de la suite « agents pour PME ». Il transforme le brouillon de relance unique d'agent-factures en un **processus de recouvrement suivi dans le temps**, sous contrôle humain.

Il cible :

- **les recruteurs (postes d'AI Product Manager)** : un partage clair des rôles (le code décide, le modèle rédige, le code vérifie), une personnalisation mesurée à l'aveugle, un arbitrage de modèle prouvé par la mesure, et la composition de trois produits ;
- **les clients PME** : moins d'impayés oubliés, des relances au bon moment et au bon ton, aucun montant faux, aucun envoi sans validation.

L'entreprise fictive utilisatrice reste **Atelier Lumière SAS**.

## 2. Choix techniques

| Sujet | Choix | Raison |
|---|---|---|
| Langage | Python ≥ 3.12, uv | Cohérence avec les projets 1 et 2 |
| Dépôt | Nouveau dépôt `agent-relances` | Trois produits autonomes et composables |
| Source des factures | Base d'agent-factures, **en lecture seule** (chemin `AGENT_FACTURES_DB`), agent-factures installé comme dépendance depuis GitHub | agent-factures reste la seule source de vérité sur les factures et les paiements |
| Architecture IA | **Le code décide, le modèle rédige, le code vérifie** | Les règles de recouvrement sont déterministes ; seule la rédaction demande un modèle ; un montant faux est inacceptable |
| Modèle de rédaction | Claude Sonnet 5 (`claude-sonnet-5`) par défaut, comparé à Claude Haiku 4.5 (`claude-haiku-4-5`) | Choix tranché par la mesure |
| Modèle juge (évaluation) | Claude Sonnet 5 | Cohérence avec le projet 2 |
| Sorties du modèle | Structurées (objet, corps), validées par Pydantic | Pas d'analyse de texte libre |
| Validation | Dépôt d'un brouillon `.eml`, **jamais d'envoi** | Aucun risque d'envoi par erreur |
| Interface | Streamlit, navigation en haut de page | Cohérence avec les projets 1 et 2 |
| Stockage | SQLite (base propre : fiches clients, historique des relances, journal) | Zéro configuration |
| Tests | pytest, faux client Anthropic | Aucun crédit consommé par les tests |

## 3. Périmètre V1

### Inclus

1. **Lecture des factures émises non payées** dans la base d'agent-factures. Une facture marquée payée dans agent-factures sort automatiquement du circuit.
2. **Fiches clients** (base d'agent-relances) : nom (identique au champ client des factures), email, nom du contact, notes, et la case **professionnel ou particulier**. Une facture sans fiche est signalée « fiche à compléter » et n'est pas relancée.
3. **Paliers de relance**, comptés depuis l'échéance, délais modifiables :

   | Palier | Délai par défaut | Ton |
   |---|---|---|
   | 1. Rappel courtois | J+3 | « Sauf oubli de votre part… » |
   | 2. Relance ferme | J+15 | Demande d'une date de règlement |
   | 3. Dernier rappel avant mise en demeure | J+30 | Rappel des conséquences ; pour un professionnel, mention de l'indemnité forfaitaire de 40 € pour frais de recouvrement |
   | Au-delà du palier 3 | — | Pas de mise en demeure automatique : « à traiter par un humain » |

   Une facture ne passe au palier suivant que si le palier précédent a été validé et que le délai du palier suivant est atteint.
4. **Regroupement** : toutes les factures en retard d'un même client sont relancées dans **un seul mail**, au palier le plus avancé parmi elles.
5. **Délai minimal de 7 jours** entre deux relances validées pour un même client.
6. **Profil du client**, calculé à partir des factures payées des 12 derniers mois dans agent-factures (retard = date de paiement − échéance) :
   - **bon payeur** : retard moyen ≤ 5 jours ;
   - **occasionnel** : retard moyen de 6 à 20 jours, ou client sans historique ;
   - **retardataire chronique** : retard moyen > 20 jours, ou au moins 2 factures en retard en même temps.
7. **Ton cible** : le code déduit du palier et du profil un ton cible (léger, neutre, ferme) :

   | Palier \ Profil | Bon payeur | Occasionnel | Retardataire chronique |
   |---|---|---|---|
   | 1 | léger | léger | neutre |
   | 2 | neutre | neutre | ferme |
   | 3 | ferme | ferme | ferme |

8. **Rédaction** par le modèle à partir de faits structurés : client, contact, profil, palier, ton cible, factures (numéro, montant TTC, échéance, jours de retard), total dû, nom de l'entreprise, mention légale éventuelle. Sortie : objet et corps.
9. **Vérification par le code** de chaque brouillon :
   - chaque numéro de facture, chaque montant TTC et chaque échéance figurent exactement ; le total figure s'il y a plusieurs factures ;
   - aucun autre montant ni aucune autre date ;
   - au palier 3 : mention des 40 € présente pour un professionnel, absente pour un particulier ; absente aux paliers 1 et 2.
   En cas d'échec : une nouvelle tentative avec la liste des erreurs, puis le **modèle de texte de secours** (déterministe), signalé comme tel.
10. **Validation humaine** : relire, modifier, **valider** (dépôt `.eml`, palier enregistré à la date du jour, prochaine relance planifiée), **reporter** de N jours, **passer à un humain**. Le brouillon modifié est revérifié avant le dépôt ; une erreur bloquante (montant faux) est affichée et le dépôt refusé.
11. **Historique des relances** : facture(s), palier, date, ton, brouillon, source (modèle ou secours), décision.
12. **Date du jour réglable** (`TODAY` dans `.env`, aujourd'hui par défaut), pour la démonstration et l'évaluation.
13. Suivi du **coût et du temps** par relance.

### Exclus (V2 et au-delà)

Lecture des réponses clients (via agent-emails), priorisation de la trésorerie et score de risque, envoi réel, mise en demeure, import CSV, pénalités de retard calculées, multi-devise, multi-utilisateur.

## 4. Architecture

```
agent-relances/
├── src/agent_relances/
│   ├── models.py         # Client, Profile, Tone, Stage, Reminder, ReminderDraft, CheckResult
│   ├── sources.py        # lecture des factures émises non payées et de l'historique de paiement (agent-factures)
│   ├── rules/
│   │   ├── profile.py    # profil du client
│   │   ├── schedule.py   # paliers, regroupement, délai minimal, ton cible → relances dues
│   │   └── legal.py      # mentions légales selon palier et type de client
│   ├── drafting/
│   │   ├── llm.py        # appel structuré + nouvelle tentative + suivi de l'usage
│   │   ├── writer.py     # rédaction par le modèle
│   │   ├── checker.py    # vérification des faits
│   │   └── fallback.py   # modèle de texte de secours
│   ├── runner.py         # « préparer les relances du jour » → brouillons vérifiés
│   ├── storage/          # SQLite : clients, relances, journal
│   └── web.py            # éléments d'interface partagés
├── app/                  # main.py + app_pages/
├── demo/seed.py          # base de démonstration (factures, paiements, fiches)
├── evals/                # generate.py, dataset/, scoring.py, judge.py, run.py, results/
└── tests/
```

**Frontières :**
- `rules/` ne contient aucun appel au modèle : ce sont des fonctions pures, testées exhaustivement.
- `drafting/` ne décide rien : il reçoit une relance due (faits et ton cible) et rend un brouillon vérifié.
- L'accès à agent-factures est isolé dans `sources.py`, en lecture seule.
- Seule l'interface écrit une décision humaine en base.

## 5. Flux de données

```
base agent-factures (lecture seule) ─► factures émises non payées + historique de paiement
                                              │
fiches clients ─► profils ─► paliers, regroupement, délai minimal, ton cible (code)
                                              │
                                     relances dues du jour
                                              │
                        rédaction (modèle) ─► vérification (code) ─► échec ? nouvelle tentative ─► secours
                                              │
                         humain : valider (dépôt .eml) / modifier / reporter / passer à un humain → journal
```

**Statuts d'une relance :** `a_valider` · `validee` · `reportee` · `a_traiter_humain` · `annulee` (facture payée entre-temps).

## 6. Gestion des erreurs

| Cas | Comportement |
|---|---|
| Base d'agent-factures introuvable | Message clair, rien n'est préparé |
| Clé d'API absente | Relances préparées avec le modèle de secours, signalé dans l'interface |
| Erreur d'API ou sortie invalide | Une nouvelle tentative, puis modèle de secours |
| Vérification échouée deux fois | Modèle de secours |
| Brouillon modifié par l'humain avec un fait faux | Erreur affichée, dépôt refusé |
| Facture payée entre la préparation et la validation | Relance annulée au moment de valider |
| Fiche client manquante ou sans email | « Fiche à compléter », pas de relance |

## 7. Interface

Navigation en haut de page : **Relances du jour** · **Clients** · **Tableau de bord** · **Journal ▾** (Relances validées · À traiter par un humain · Historique).

- **Relances du jour** : bouton « Préparer les relances du jour » ; liste triée par montant dû ; pour chaque relance, le client, le profil, le palier, le ton cible, les factures, le brouillon modifiable, la source (modèle ou secours), et les actions Valider, Reporter (nombre de jours), Passer à un humain.
- **Clients** : fiches (création et modification), profil calculé, historique des relances, alerte « fiche à compléter ».
- **Tableau de bord** : montant en retard, nombre de factures par palier, relances validées, **montant encaissé après relance** (factures payées après au moins une relance validée), délai moyen entre la première relance et le paiement, coût moyen par relance.

## 8. Tests

- `rules` : paliers (J+2 rien, J+3 palier 1, J+15, J+30, au-delà humain), passage de palier seulement après validation du précédent, regroupement, délai minimal de 7 jours, profils (bornes 5 et 20 jours, sans historique, 2 factures en retard), ton cible, mention légale (professionnel, particulier, paliers 1 et 2).
- `checker` : montant faux, numéro manquant, échéance manquante, total manquant, date ou montant inventés, mention légale absente ou en trop ; formats français (`1 200,50 €`, `15/10/2026`, `15 octobre 2026`).
- `drafting` avec faux client : succès, nouvelle tentative, recours au secours, usage comptabilisé.
- `sources` : factures émises non payées seulement, payées exclues, historique de paiement.
- `storage` : fiches, historique, statuts, décisions humaines jamais écrasées.
- Interface (AppTest) : chaque page s'affiche ; valider, modifier (y compris un fait faux refusé), reporter, passer à un humain ; clé d'API absente.

## 9. Évaluations

- `evals/generate.py` génère **30 scénarios** (fiches clients, historique de paiement, factures en retard, date du jour) avec les réponses attendues : relances dues, palier, regroupement, profil, ton cible, mention légale.
  - Couverture : chaque case du tableau palier × profil, professionnels et particuliers, regroupements de 2 à 3 factures, délai minimal, fiche manquante, facture payée, montants avec décimales et milliers.
- `evals/run.py --model … --max-cost …` mesure :
  - l'exactitude des **règles** ;
  - l'exactitude des **faits** après vérification, et le taux de réussite **dès le premier essai** ;
  - le **ton reconnu à l'aveugle** : le juge lit la relance sans connaître le palier ni le profil et choisit léger, neutre ou ferme ;
  - la **note du juge** (1 à 5) selon une grille publiée : clarté, politesse, fermeté adaptée, appel à l'action ;
  - le **recours au modèle de secours** ;
  - le coût et le temps moyens par relance.
- **Expérience :** rédaction avec Sonnet puis avec Haiku, sur les mêmes scénarios.
- Chaque run réel est plafonné (`--max-cost`) et soumis à l'accord de l'utilisateur.

## 10. Présentation portfolio

- **README** : pitch métier et GIF de démo, résultats mesurés, schéma, section « Le code décide, le modèle rédige, le code vérifie », grille du juge, liens vers les projets 1 et 2.
- **Document de pitchs**, **CV** et **LinkedIn** : ajout du projet 3.

## 11. Critères de réussite

| Critère | Seuil |
|---|---|
| Règles (relance, palier, regroupement, mention) | 100 % |
| Faits exacts dans les brouillons déposés | 100 % |
| Ton reconnu à l'aveugle | ≥ 85 % |
| Note moyenne du juge | ≥ 4/5 |
| Recours au modèle de secours | ≤ 5 % |
| Coût par relance | ≤ 0,02 $ |
| Tests | Tous verts, sans clé d'API |
| Démo | Lancée par un inconnu en moins de 5 minutes grâce au README |
