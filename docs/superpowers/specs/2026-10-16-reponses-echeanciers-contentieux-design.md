# agent-relances V2 : réponses des clients, échéanciers et contentieux — Design

**Date :** 2026-10-16
**Statut :** décisions de l'utilisateur intégrées, en attente de sa relecture
**Projets liés :** agent-relances V1 (`docs/superpowers/specs/2026-10-15-agent-relances-design.md`), [agent-emails](https://github.com/micouaidan55/agent-emails), [agent-factures](https://github.com/micouaidan55/agent-factures)

## 1. Intention

Fermer la boucle de recouvrement. Aujourd'hui, agent-relances prépare des relances, mais ignore ce que le client répond. Dans la pratique de l'utilisateur (gérant de deux centres de santé pendant plus de cinq ans), une réponse à une relance ouvre une négociation qui se termine de deux façons :

- **un échéancier** est convenu et suivi jusqu'au dernier versement ;
- **ou le dossier part en contentieux**, transmis à un avocat avec les factures et l'historique des relances.

Cette V2 fait les trois : comprendre les réponses des clients, gérer les échéanciers, préparer le dossier pour l'avocat. C'est aussi le maillon qui relie concrètement les trois projets de la suite : une réponse arrive par email, elle agit sur les relances, qui s'appuient sur les factures.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
1. Reconnaître une réponse de client à une relance, la rattacher au bon client et aux bonnes factures, et proposer l'action adaptée.
2. Proposer, faire valider et suivre un échéancier qui respecte **toujours** la règle de l'entreprise.
3. Relancer une mensualité manquée, et passer en contentieux après 3 relances sans paiement.
4. Préparer un dossier de contentieux complet pour l'avocat, avec un brouillon de mail.

**Non-objectifs (V2)**
- Envoi réel de mails, prélèvement ou paiement en ligne.
- Négociation automatique en plusieurs allers-retours : l'agent propose, un humain décide.
- Calcul de pénalités ou d'intérêts de retard.
- Suivi de la procédure judiciaire après la transmission à l'avocat.

**Anti-objectif (ce qui ferait échouer le projet même s'il fonctionne)**
- **Un échéancier proposé, déposé ou enregistré qui ne respecte pas la règle.** C'est la priorité absolue de l'utilisateur. D'où la règle de conception : l'échéancier est calculé et vérifié uniquement par le code ; le modèle ne fixe jamais un nombre de mensualités, un montant ni une date.
- Agir sur une réponse frauduleuse (par exemple « j'ai payé, voici notre nouveau RIB ») : la détection de fraude d'agent-emails s'applique avant toute action.

## 3. Règles métier (données par l'utilisateur)

**Règle d'échéancier**
- 4 mensualités au maximum ;
- chaque mensualité est d'au moins 100 € ;
- exception : pour un montant total inférieur à 200 €, un échéancier en 2 fois est possible, même si chaque versement est inférieur à 100 € ;
- donc, pour un total T : nombre maximal de mensualités = 2 si T < 200 €, sinon min(4, ⌊T / 100⌋) ; si ce maximum vaut 1, aucun échéancier n'est possible (T < 200 € donne toujours 2, donc ce cas ne survient pas, sauf montant nul).

**Suivi et contentieux**
- Une mensualité non payée à sa date est relancée.
- Après **3 relances sans paiement** d'une même mensualité, le dossier passe en contentieux.
- Une facture déjà au-delà du dernier palier de relance (cas « à traiter par un humain » de la V1) peut aussi être passée en contentieux par l'humain.
- Le contentieux est transmis à **un avocat**, avec **les factures et l'historique des relances**.

## 4. Ce qui est décidé

### 4.1 Où ça vit
Une V2 d'agent-relances. agent-relances dépend désormais aussi d'agent-emails (dépendance git, comme agent-factures) pour réutiliser :
- la lecture des fichiers `.eml` (`parse_eml`) et la neutralisation du contenu non fiable ;
- les signaux de fraude (`fraud_signals`, `is_suspicious`).

agent-emails, de son côté, reconnaît une réponse à une relance à la seule présence d'une référence `[Réf. R-NNNNN]` dans l'objet, sans dépendre d'agent-relances (pas de dépendance circulaire), et la marque « traitée par agent-relances » au lieu de préparer sa propre réponse. Ce petit changement d'agent-emails fait partie du projet, en dernière étape.

### 4.2 Source des réponses
- Les réponses sont lues dans un dossier de fichiers `.eml` configurable (`AGENT_RELANCES_INBOX`), qui peut être la même boîte de réception que celle d'agent-emails.
- Une réponse déjà traitée n'est jamais retraitée (identifiant de message mémorisé).

### 4.3 Rattachement d'une réponse
Un client de messagerie remplace l'identifiant d'un brouillon au moment de l'envoi : l'en-tête `In-Reply-To` n'est donc pas fiable (décision du constructeur). Les relances déposées portent désormais une **référence dans leur objet**, par exemple `[Réf. R-00042]`, que les clients conservent en répondant (« Re: … »). Dans cet ordre :
1. l'objet contient une référence `R-NNNNN` d'une relance déposée ;
2. sinon, l'adresse de l'expéditeur correspond à l'email d'une fiche client (comparaison sans casse) ;
3. sinon, la réponse est classée « non rattachée » et présentée à l'humain, qui peut la rattacher à la main.

Une réponse se rattache à un **client** ; les actions portent sur le dossier de ce client (voir §4.5).

### 4.4 Compréhension de la réponse (Claude)
Sortie structurée :
- intention : `paiement_annonce`, `promesse_date`, `demande_echeancier`, `contestation`, `autre` ;
- date promise (si l'intention est `promesse_date`), telle qu'écrite et normalisée en date ;
- nombre de mensualités demandé (si le client en propose un) ;
- numéros de facture cités ;
- un résumé d'une phrase.

Le contenu de l'email est une donnée non fiable, neutralisé comme dans agent-emails. Le modèle de compréhension est Claude Haiku 4.5 par défaut, à confirmer par la mesure (voir §6).

### 4.5 Actions proposées (un humain confirme toujours)
**Portée (décision de l'utilisateur pour l'échéancier ; du constructeur pour le reste)** : une action porte sur **toutes les factures impayées du client** à la date de la réponse. Pour une pause ou un report, cela évite de relancer une facture pendant qu'on discute d'une autre ; une réponse non rattachée n'agit sur rien.

| Intention | Action proposée |
|---|---|
| `paiement_annonce` | Relances reportées de 7 jours en attente du paiement dans agent-factures ; si rien n'arrive, les relances reprennent |
| `promesse_date` | Relances reportées à la date promise + 3 jours |
| `demande_echeancier` | Échéancier calculé par le code (§4.6) et brouillon de réponse au client présentant l'échéancier |
| `contestation` | Relances suspendues sans date de fin (nouvel état « suspendu »), dossier « à traiter par un humain », qui peut lever la suspension |
| `accord_echeancier` (le client accepte une proposition en cours) | Échéancier activé : dates fixées, mail de confirmation préparé (§4.6) |
| `autre` ou réponse suspecte de fraude | Dossier « à traiter par un humain », aucune action automatique |

L'intention `accord_echeancier` s'ajoute à la liste du §4.4. L'humain peut aussi enregistrer l'accord du client à la main (accord donné par téléphone, par exemple).

### 4.6 Échéancier
- **Montant couvert (décision de l'utilisateur)** : l'échéancier porte sur **toutes les factures impayées du client** à la date de la proposition ; T est la somme de leurs montants TTC. Une seule proposition ou un seul échéancier en cours par client.
- **Calcul (code uniquement)** : nombre de mensualités = le nombre demandé par le client s'il respecte la règle, sinon le maximum autorisé ; une demande d'une seule fois n'est pas un échéancier (elle est traitée comme une promesse de paiement ou « autre ») ; sous 200 €, l'échéancier est toujours en 2 fois. Montants égaux arrondis au centime ; le dernier versement absorbe l'écart d'arrondi.
- **Cycle de vie** :
  1. *proposé* : l'agent prépare un brouillon de proposition au client (nombre de mensualités et montant de chacune, première échéance 30 jours après son accord, sans date précise) ; l'humain valide et le brouillon est déposé ;
  2. *en cours* (**décision de l'utilisateur : démarre à l'accord du client**) : quand le client accepte (réponse `accord_echeancier` confirmée par l'humain, ou accord saisi à la main), les dates sont fixées : première échéance 30 jours après la date de l'accord, puis tous les mois à la même date ; un brouillon de confirmation avec les dates exactes est préparé ;
  3. *soldé* : toutes les mensualités sont reçues, ou toutes les factures couvertes sont marquées payées dans agent-factures ;
  4. *rompu* : passage en contentieux (voir plus bas).
  Si le client refuse ou ne répond pas à une proposition, l'humain peut l'annuler ; les relances V1 reprennent.
- **Pendant un échéancier proposé ou en cours**, les factures couvertes sortent du cycle de relances de la V1 (nouvel état « sous échéancier ») ; une nouvelle facture impayée du même client, apparue après la proposition, suit le cycle normal.
- **Vérification (code)** : avant tout dépôt ou enregistrement, l'échéancier est revérifié contre la règle ; les brouillons de proposition et de confirmation sont vérifiés par le contrôle des faits de la V1, étendu : chaque montant (et, pour la confirmation, chaque date) de l'échéancier doit y figurer exactement, et aucun autre montant ni aucune autre date. La proposition peut mentionner le nombre de mensualités demandé par le client s'il diffère (« vous nous demandiez 6 mois ; nous pouvons vous proposer 4 mensualités ») : un nombre de mois n'est pas un montant.
- **Modification humaine** : l'humain peut changer le nombre de mensualités ; un choix hors de la règle est refusé avec la raison affichée.
- **Suivi** : l'humain marque chaque mensualité comme reçue dans agent-relances (agent-factures ne connaît que les paiements complets). Le **reste dû** = total des factures couvertes − mensualités reçues. Quand toutes les mensualités sont reçues, l'interface rappelle de marquer les factures payées dans agent-factures.
- **Mensualité manquée** : relances de la mensualité avec les paliers et les tons de la V1, comptés depuis la date d'échéance de la mensualité (J+3, J+15, J+30), chacune validée par l'humain, avec le délai minimal de 7 jours par client. Le contrôle des faits s'applique (montant et date de la mensualité). **Si la mensualité reste impayée 7 jours après la 3e relance validée, le passage en contentieux est proposé** à l'humain.

### 4.7 Dossier de contentieux
- Contenu :
  - un PDF « Dossier de recouvrement » : client, factures (numéro, date, montant TTC, échéance), montant restant dû, chronologie datée des relances, réponses du client résumées, échéancier convenu avec les mensualités reçues et manquées ;
  - les fichiers des factures enregistrés dans agent-factures, quand ils existent ;
  - un brouillon de mail `.eml` à l'avocat (adresse dans `AVOCAT_EMAIL`), jamais envoyé.
- **Périmètre** : toutes les factures du client couvertes par l'échéancier rompu, ou la facture passée à un humain après le dernier palier, plus toute autre facture impayée du même client à cette date.
- Les fichiers de factures sont ceux dont agent-factures garde le chemin (`source_file`) ; ils sont copiés s'ils sont lisibles, sinon le PDF le signale.
- Le tout est rangé dans un dossier `data/contentieux/<client>_<date>/`.
- Le dossier passe au statut « en contentieux » et sort de tout circuit automatique.

### 4.8 Interface
Ajouts à l'application agent-relances :
- page **Réponses clients** : réponses à traiter, avec l'intention comprise, le rattachement et l'action proposée à confirmer ;
- page **Échéanciers** : échéanciers en cours, mensualités à venir, reçues et en retard ;
- page **Contentieux** : dossiers préparés, avec le lien vers chaque dossier ;
- les relances déposées affichent leur référence `R-NNNNN` ;
- tableau de bord : nombre d'échéanciers en cours, montant restant à percevoir sur échéancier, dossiers en contentieux.

## 5. Contraintes

- Mêmes contraintes techniques que la V1 : Python ≥ 3.12, uv, Streamlit, SQLite, tests sans clé d'API, rien n'est jamais envoyé.
- agent-factures reste la seule source de vérité sur les factures et les paiements complets ; il est toujours ouvert en lecture seule.
- Toute réponse de client est une donnée non fiable.

## 6. Mesure

- **Règle d'échéancier** : test exhaustif sur tous les montants de 0,01 € à 20 000 € (pas de 0,01 € jusqu'à 1 000 €, puis par euro) et tous les nombres demandés de 1 à 12 : **100 % conformes**, sinon le projet échoue.
- **Jeu de réponses générées** (environ 40) : intention correcte ≥ 95 %, date promise correcte ≥ 95 %, nombre de mois demandé correct ≥ 95 %, rattachement correct 100 % sur les cas rattachables, toutes les réponses frauduleuses bloquées.
- **Brouillons d'échéancier** : 100 % des montants et dates exacts après vérification.
- Comparaison Haiku / Sonnet pour la compréhension des réponses, avec un plafond de 1 $ par run et l'accord de l'utilisateur.
- Le jeu de réponses est généré par du code (comme le jeu de la V1), avec les réponses attendues écrites d'après ce document.

## 7. Laissé au constructeur

- Le découpage exact des modules et du schéma SQLite (en suivant les conventions de la V1).
- La forme du PDF de recouvrement et du mail à l'avocat.
- Le contenu exact des prompts, à condition de respecter §4.4 et la neutralisation.
- La génération du jeu d'évaluation (cas pièges : réponse polie mais contestataire, échéancier demandé en 6 fois, date écrite en toutes lettres, réponse d'une autre adresse que la fiche, réponse frauduleuse avec nouveau RIB).

## Évolution : dérogation exceptionnelle

La règle d'échéancier admet une dérogation humaine au plafond de 4 mensualités : jusqu'à 12 mensualités (`EXCEPTION_MAX_INSTALLMENTS`), le minimum de 100 € par mensualité restant obligatoire (maximum = min(12, ⌊total ÷ 100⌋), et 2 en dessous de 200 €). Un motif écrit non vide est obligatoire ; seul un humain la déclenche (page Réponses), Claude et le code ne proposent jamais plus de 4. Le motif est stocké sur l'échéancier (`plans.exception_reason`), journalisé, affiché sur la page Échéanciers et repris dans le dossier de contentieux ; chaque revérification du plan (brouillons, dépôt, activation) applique la dérogation si, et seulement si, le motif existe.
