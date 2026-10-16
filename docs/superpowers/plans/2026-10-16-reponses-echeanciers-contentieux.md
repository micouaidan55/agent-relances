# agent-relances V2 — Réponses clients, échéanciers et contentieux — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** fermer la boucle de recouvrement : comprendre les réponses des clients aux relances, proposer et suivre des échéanciers conformes à la règle de l'entreprise, et préparer le dossier de contentieux pour l'avocat.

**Architecture :** la règle d'échéancier et le suivi des mensualités sont des fonctions pures (`rules/installments.py`) ; un nouveau magasin (`storage/recovery.py`) garde réponses, pauses, échéanciers et dossiers ; `replies/` lit, rattache, comprend (Claude Haiku) et applique les réponses ; `drafting/plans.py` rédige et vérifie les mails d'échéancier ; `disputes/` construit le dossier de contentieux ; l'interface gagne trois pages. agent-emails devient une dépendance (lecture des `.eml`, neutralisation, signaux de fraude) et reçoit, en dernière tâche, la reconnaissance des réponses aux relances.

**Tech Stack :** Python 3.12, uv, anthropic ≥ 1.9.0 (`messages.parse`), Pydantic 2, SQLite, Streamlit 1.64, reportlab (PDF), pytest.

**Spec :** `docs/superpowers/specs/2026-10-16-reponses-echeanciers-contentieux-design.md` (et la V1 : `docs/superpowers/specs/2026-10-15-agent-relances-design.md`).

## Global Constraints

- Mêmes contraintes que la V1 : Python ≥ 3.12, uv (`uv run …`), tests sans clé d'API (faux client de `tests/fakes.py`), interface et textes en français, `md_escape` pour tout texte non fiable affiché en Markdown, rien n'est jamais envoyé (dépôt de `.eml` seulement), agent-factures ouvert en lecture seule.
- Dépendance ajoutée : `agent-emails = { git = "https://github.com/micouaidan55/agent-emails" }` (et `reportlab>=5.0.1`).
- **Règle d'échéancier** : 4 mensualités au maximum ; au moins 100 € par mensualité ; si le total est inférieur à 200 €, échéancier en 2 fois (même sous 100 € par versement). Maximum = 2 si T < 200 €, sinon min(4, ⌊T / 100⌋). Une demande d'une seule fois n'est pas un échéancier. Montants égaux arrondis au centime, le dernier absorbe l'écart.
- **L'échéancier est calculé et vérifié uniquement par le code**, jamais par le modèle ; il est revérifié avant tout dépôt ou enregistrement. Un échéancier non conforme ne doit jamais être proposé, déposé ni enregistré.
- L'échéancier couvre **toutes les factures impayées du client** à la date de la proposition ; un seul échéancier proposé ou en cours par client.
- Cycle : *proposé* → *en cours* à l'accord du client (dates fixées : première échéance **30 jours après l'accord**, puis tous les mois à la même date) → *soldé* ou *rompu* ; *annulé* par l'humain.
- Mensualité manquée : relances J+3, J+15, J+30 depuis sa date d'échéance, chacune validée par l'humain, délai minimal de 7 jours par client ; contentieux proposé **7 jours après la 3e relance validée** restée sans paiement.
- Actions sur réponse (un humain confirme toujours) : `paiement_annonce` → report de 7 jours ; `promesse_date` → report jusqu'à la date promise + 3 jours ; `demande_echeancier` → proposition ; `accord_echeancier` → activation ; `contestation` → suspension sans fin + humain ; `autre` ou réponse suspecte de fraude → humain, aucune action. Une action porte sur toutes les factures impayées du client.
- Références dans l'objet des mails déposés : relance `[Réf. R-00042]` (5 chiffres), échéancier `[Réf. E-00007]`.
- Modèle de compréhension des réponses : `claude-haiku-4-5` par défaut (comparé à `claude-sonnet-5`, plafond 1 $ par run, accord de l'utilisateur).
- Commits : messages en français, terminés exactement par `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Réponse d'un client qui n'a plus aucune facture impayée (« j'ai payé » alors que tout est soldé) : aucune pause ni échéancier, la réponse est seulement archivée comme « sans objet ». Test en Task 6.
2. « Accord » reçu alors qu'aucune proposition n'est en attente pour ce client (ou proposition annulée) : rien n'est activé, la réponse passe à l'humain. Test en Task 6.
3. Une facture couverte est marquée payée dans agent-factures pendant l'échéancier : le reste dû et le statut restent cohérents (soldé si toutes les factures couvertes sont payées). Test en Task 7.
4. L'humain saisit un nombre de mensualités hors règle (5, ou 3 pour 250 €) : refus avec la raison, rien n'est enregistré. Test en Task 1 et Task 9.
5. Date promise écrite de façon vague (« fin du mois », « dès que possible ») ou dans le passé : pas de report automatique à une date inventée ; l'action devient « à traiter par un humain ». Test en Task 5.

---

### Task 1 : règle d'échéancier (code pur)

**Files :** Create `src/agent_relances/rules/installments.py` ; Test `tests/test_installments.py`

**Interfaces — Produces :**
- `MAX_INSTALLMENTS = 4`, `MIN_INSTALLMENT = Decimal("100")`, `SMALL_TOTAL = Decimal("200")`, `FIRST_DUE_DAYS = 30`
- `class PlanRuleError(ValueError)`
- `max_installments(total: Decimal) -> int`
- `plan_amounts(total: Decimal, requested: int | None) -> list[Decimal]` — `requested` dans la règle → ce nombre ; hors règle ou `None` → le maximum ; `requested == 1` ou `total < Decimal("0.02")` → `PlanRuleError`
- `plan_violations(amounts: list[Decimal], total: Decimal) -> list[str]` — liste vide si conforme (somme exacte, nombre ≤ maximum, ≥ 2 versements, chaque versement ≥ 100 € sauf total < 200 €, centimes entiers)
- `installment_dates(agreed_on: date, count: int) -> list[date]` — première = `agreed_on + 30 jours`, puis même jour chaque mois (ramené au dernier jour du mois si besoin)

- [ ] **Step 1 : tests**

```python
@pytest.mark.parametrize("total,requested,expected", [
    ("1200", None, ["300.00"] * 4), ("1200", 2, ["600.00"] * 2), ("250", None, ["125.00"] * 2),
    ("150", None, ["75.00"] * 2), ("150", 4, ["75.00"] * 2), ("450", 6, ["112.50"] * 4),
    ("399.99", None, ["133.33", "133.33", "133.33"]), ("1000", 3, ["333.33", "333.33", "333.34"]),
])
def test_plan_amounts(total, requested, expected): ...  # plan_amounts(Decimal(total), requested) == [Decimal(e) for e in expected]

def test_one_payment_is_not_a_plan(): ...        # plan_amounts(Decimal("500"), 1) lève PlanRuleError
def test_max_installments(): ...                 # 199.99 → 2 ; 200 → 2 ; 299.99 → 2 ; 300 → 3 ; 400 → 4 ; 20000 → 4

def test_rule_holds_for_every_amount():          # exhaustif : 0,02 € à 1 000 € au centime, puis 1 001 € à 20 000 € à l'euro,
    ...                                          # requested ∈ {None, 2..12} : plan_violations(plan_amounts(T, r), T) == [] et sum == T

@pytest.mark.parametrize("amounts,total", [(["100", "100", "100", "100", "100"], "500"),   # 5 mensualités
                                           (["200", "50"], "250"), (["90", "90", "90"], "270"), (["600"], "600")])
def test_violations_detected(amounts, total): ...  # plan_violations(...) non vide

def test_installment_dates(): ...  # agreed 2026-10-16, 4 → [2026-11-15, 2026-12-15, 2027-01-15, 2027-02-15] ;
                                   # agreed 2026-12-01, 3 → [2026-12-31, 2027-01-31, 2027-02-28]
```

- [ ] **Step 2 :** `uv run pytest tests/test_installments.py -q` → FAIL (module absent).
- [ ] **Step 3 :** implémenter les signatures ci-dessus (calcul en `Decimal`, quantize au centime, `ROUND_DOWN` pour les n−1 premiers versements).
- [ ] **Step 4 :** `uv run pytest -q` → tout passe (le test exhaustif doit rester sous 20 s).
- [ ] **Step 5 :** commit `feat: règle d'échéancier calculée et vérifiée par le code`.

---

### Task 2 : modèles et magasin de recouvrement

**Files :** Modify `src/agent_relances/models.py`, `src/agent_relances/storage/db.py` ; Create `src/agent_relances/storage/recovery.py` ; Test `tests/test_recovery_store.py`

**Interfaces — Produces :**
- `models.ReplyIntent` (StrEnum) : `PAYMENT_ANNOUNCED="paiement_annonce"`, `PROMISED_DATE="promesse_date"`, `PLAN_REQUEST="demande_echeancier"`, `PLAN_AGREEMENT="accord_echeancier"`, `DISPUTE="contestation"`, `OTHER="autre"`
- `models.ReplyReading(BaseModel)` (sortie du modèle) : `intent: ReplyIntent`, `promised_date: str | None` (AAAA-MM-JJ), `requested_installments: int | None`, `invoice_numbers: list[str]`, `summary: str`
- `models.ReplyStatus` : `TO_REVIEW="a_traiter"`, `APPLIED="appliquee"`, `DISMISSED="classee"`, `UNMATCHED="non_rattachee"`
- `models.ActionKind` : `POSTPONE="report"`, `SUSPEND="suspension"`, `PROPOSE_PLAN="proposition_echeancier"`, `ACTIVATE_PLAN="activation_echeancier"`, `HUMAN="humain"`, `NONE="sans_objet"`
- `models.PlanStatus` : `PROPOSED="propose"`, `ACTIVE="en_cours"`, `SETTLED="solde"`, `BROKEN="rompu"`, `CANCELLED="annule"`
- `models.Installment(number: int, amount: Decimal, due_date: date | None, received_on: date | None)`
- `models.Plan(id, client_key, invoice_ids: list[int], total: Decimal, status: PlanStatus, proposed_on: date, agreed_on: date | None, installments: list[Installment])` avec `.remaining -> Decimal` (total − mensualités reçues)
- Tables : `replies` (id, message_id unique, client_key nullable, reminder_ref, match_method, sender, subject, received_at, body, intent, promised_date, requested_installments, summary, suspicious, proposed_action, action_until, status, cost_usd, created_on), `holds` (id, client_key, kind 'report'|'suspension', until nullable, reason, created_on, released_on), `plans`, `plan_invoices`, `installments`, `installment_reminders` (plan_id, number, stage, decided_on, eml_path), `disputes` (id, client_key, folder, created_on, total_due).
- `RecoveryStore(conn)` : `known_reply_ids() -> set[str]`, `add_reply(...) -> int`, `get_reply(id)`, `list_replies(statuses=None)`, `set_reply_status(id, status) -> bool` (seulement depuis `a_traiter`) ; `add_hold(client_key, kind, until, reason, today) -> int`, `active_holds(today) -> dict[str, date | None]` (`None` = suspension sans fin ; une pause expirée n'est pas active), `release_hold(id, today)` ; `create_plan(client_key, invoice_ids, total, amounts, today) -> int` (lève `ValueError` si un plan proposé ou en cours existe pour ce client ou si `plan_violations` n'est pas vide), `get_plan(id)`, `open_plan_for(client_key) -> Plan | None`, `activate_plan(id, agreed_on) -> bool` (fixe les dates via `installment_dates`, seulement depuis *proposé*), `mark_received(plan_id, number, on) -> bool`, `set_plan_status(id, status, today) -> bool`, `list_plans(statuses=None)`, `planned_invoice_ids() -> set[int]` (plans proposés ou en cours) ; `record_installment_reminder(plan_id, number, stage, decided_on, eml_path)`, `installment_reminders(plan_id) -> dict[int, list[tuple[int, date]]]` ; `add_dispute(client_key, folder, total_due, today) -> int`, `list_disputes()`.

- [ ] **Step 1 : tests** — un test par groupe : aller-retour d'une réponse et dédoublonnage par `message_id` ; pause expirée non active, suspension sans fin active, `release_hold` ; `create_plan` refuse un 2e plan ouvert pour le même client et refuse des montants non conformes (`["100"]*5` pour 500) ; `activate_plan` fixe les dates (accord 2026-10-16 → première 2026-11-15) et refuse une seconde activation ; `mark_received` puis `.remaining` ; `planned_invoice_ids` ignore les plans annulés ; migration : `connect` sur une base V1 existante ajoute les nouvelles tables sans perte.
- [ ] **Step 2 :** FAIL. **Step 3 :** implémenter (tables en `CREATE TABLE IF NOT EXISTS`, montants en TEXT, décisions atomiques `UPDATE … WHERE status = ?`). **Step 4 :** `uv run pytest -q` vert.
- [ ] **Step 5 :** commit `feat: magasin des réponses, pauses, échéanciers et contentieux`.

---

### Task 3 : relances V1 et pauses / échéanciers ; référence dans l'objet

**Files :** Modify `src/agent_relances/rules/schedule.py`, `src/agent_relances/prepare.py`, `src/agent_relances/outbox.py` ; Test `tests/test_rules_schedule.py`, `tests/test_prepare.py`

**Interfaces :**
- Consumes : `RecoveryStore.active_holds`, `planned_invoice_ids` (Task 2)
- Produces : `due_reminders(..., holds: dict[str, date | None] | None = None, planned_invoice_ids: set[int] | None = None)` — un client en pause active est entièrement ignoré ; une facture couverte par un échéancier ne suit plus le cycle V1 ; `prepare_today(..., recovery: RecoveryStore | None = None)` les transmet ; `outbox.reference(kind: str, number: int) -> str` (`reference("R", 42) == "R-00042"`) ; `validate_reminder` ajoute ` [Réf. R-00042]` à la fin de l'objet déposé et enregistré (une seule fois, même si l'humain l'a déjà mis).

- [ ] **Step 1 : tests** — client en report jusqu'à demain : pas de relance ; report expiré : relance ; suspension sans fin : pas de relance ; facture sous échéancier ignorée mais l'autre facture du client (créée après) relancée ; objet déposé contient exactement une fois `[Réf. R-00001]` ; le contrôle des faits accepte cette référence (aucun montant ni date détectés dedans).
- [ ] **Steps 2-4 :** FAIL puis vert sur toute la suite.
- [ ] **Step 5 :** commit `feat: pauses et échéanciers respectés par les relances, référence dans l'objet`.

---

### Task 4 : lecture et rattachement des réponses

**Files :** Modify `pyproject.toml` (dépendance agent-emails, reportlab), `.env.example` (`AGENT_RELANCES_INBOX=../agent-emails/inbox`, `AVOCAT_EMAIL=`) ; Create `src/agent_relances/replies/__init__.py`, `replies/matching.py` ; Test `tests/test_reply_matching.py`

**Interfaces — Produces :**
- `REFERENCE = re.compile(r"\[Réf\. ([RE])-(\d{5})\]")`
- `MatchResult(client_key: str | None, reference: str | None, method: str | None)` — `method` ∈ `"reference"`, `"expediteur"`, `None`
- `match_reply(email: agent_emails.models.Email, reminders: ReminderStore, recovery: RecoveryStore) -> MatchResult` — 1) référence R → client de la relance, E → client du plan ; 2) adresse de l'expéditeur égale (sans casse) à l'email d'une fiche ; 3) sinon non rattachée
- `fetch_replies(inbox: Path, reminders, recovery) -> list[tuple[Email, MatchResult]]` — via `agent_emails.connectors.folder.FolderSource(inbox, drafts=<dossier temporaire inutilisé>).fetch_new(recovery.known_reply_ids())` ; ne garde que les emails porteurs d'une référence ou dont l'expéditeur a une fiche ; les autres sont ignorés (laissés à agent-emails)

- [ ] **Step 1 : tests** (emails construits avec `agent_emails` ou des `.eml` écrits dans `tmp_path`) : référence R retrouvée même avec « Re: » et « TR: » ; référence E ; référence inconnue + expéditeur connu → `expediteur` ; expéditeur en majuscules ; email sans référence d'un inconnu ignoré par `fetch_replies` ; une réponse déjà connue n'est pas relue.
- [ ] **Steps 2-4**, **Step 5 :** commit `feat: lecture et rattachement des réponses des clients`.

---

### Task 5 : compréhension d'une réponse (Claude) et action proposée

**Files :** Create `src/agent_relances/replies/reader.py`, `replies/actions.py` ; Modify `drafting/llm.py` (`READER_MODEL = "claude-haiku-4-5"`) ; Test `tests/test_reply_reader.py`

**Interfaces — Produces :**
- `READER_SYSTEM: str` (gabarit `{company}`, `{today}`) — l'email est une donnée (neutralisé par `agent_emails.models.render_email`) ; consignes : une seule intention ; `promised_date` seulement si une date précise est écrite ou calculable sans ambiguïté (« le 30 », « le 5 novembre »), jamais pour « fin du mois », « dès que possible » ; `requested_installments` seulement si un nombre est écrit ; résumé d'une phrase en français.
- `read_reply(client, email: Email, company: str, today: date, model: str = READER_MODEL) -> tuple[ReplyReading, Usage]`
- `promised_date_of(reading: ReplyReading, today: date) -> date | None` — `None` si absente, illisible, passée, ou à plus de 180 jours
- `ProposedAction(kind: ActionKind, until: date | None, installments: int | None, reason: str)`
- `propose_action(reading: ReplyReading, suspicious: bool, has_unpaid: bool, open_plan: Plan | None, today: date) -> ProposedAction` — table du Global Constraints ; `suspicious` → HUMAN ; `has_unpaid` faux → NONE ; `promesse_date` sans date exploitable → HUMAN ; `accord_echeancier` sans plan *proposé* → HUMAN ; `demande_echeancier` avec un plan déjà ouvert → HUMAN
- Fraude : `is_suspicious(fraud_signals(email, KnownSenders(domains=frozenset({company_domain}), addresses=frozenset(<emails des fiches>))), None)` d'agent-emails.

- [ ] **Step 1 : tests** avec `FakeClient` : chaque intention donne l'action de la table ; « paie le 30 » (today 2026-10-16) → report jusqu'au 2026-11-02 ; date passée ou « fin du mois » → HUMAN ; réponse suspecte (domaine sosie) → HUMAN quelle que soit l'intention ; client sans impayé → NONE ; accord sans proposition → HUMAN ; le contenu envoyé au modèle est dans `<email>` et neutralisé.
- [ ] **Steps 2-4**, **Step 5 :** commit `feat: compréhension des réponses et action proposée`.

---

### Task 6 : traitement des réponses et application après confirmation humaine

**Files :** Create `src/agent_relances/replies/process.py` ; Test `tests/test_reply_process.py`

**Interfaces :**
- Consumes : Tasks 2 à 5 ; `InvoiceSource` (factures impayées du client : ajouter `InvoiceSource.unpaid_of(client_key: str) -> list[OverdueInvoice]` qui renvoie aussi les factures non encore échues, `days_late` pouvant être ≤ 0)
- Produces :
  - `process_replies(inbox: Path, reminders, recovery, source, api, company: str, company_domain: str, today: date) -> list[int]` — lit, rattache, comprend (ou classe `non_rattachee` sans appel au modèle si non rattachée), propose et enregistre chaque réponse au statut `a_traiter` ; journalise
  - `confirm_reply(reply_id: int, reminders, recovery, source, api, company: str, today: date, installments: int | None = None) -> list[str]` — applique l'action proposée (report ou suspension via `add_hold` ; proposition : `plan_amounts` sur la somme des factures impayées du client puis `create_plan` puis brouillon de proposition (Task 8) ; activation : `activate_plan(agreed_on=today)` puis brouillon de confirmation (Task 8) ; HUMAN : `add_hold(..., "suspension", None)` + journal ; NONE : rien) et passe la réponse à `appliquee` ; renvoie des messages d'erreur (vide si succès) ; `installments` permet à l'humain d'imposer un nombre, refusé s'il est hors règle
  - `dismiss_reply(reply_id, recovery, today) -> bool`

- [ ] **Step 1 : tests** de bout en bout (base agent-factures en mémoire, `FakeClient`) : « je paierai le 30 » → après confirmation, le client n'est plus relancé jusqu'au 2026-11-02 ; « je conteste » → suspension sans fin ; « je peux payer en 6 fois » sur 450 € → plan proposé de 4 × 112,50 € couvrant toutes les factures impayées du client ; accord ensuite → plan en cours avec dates ; l'humain impose 5 mensualités → refus, rien n'est créé ; Review Focus 1 (« j'ai payé », aucun impayé) → NONE, aucune pause ; Review Focus 2 (accord sans proposition) → HUMAN ; une réponse confirmée deux fois n'agit qu'une fois.
- [ ] **Steps 2-4**, **Step 5 :** commit `feat: traitement et application des réponses des clients`.

---

### Task 7 : suivi des mensualités et propositions de contentieux

**Files :** Modify `src/agent_relances/rules/installments.py` ; Create `src/agent_relances/plans.py` ; Test `tests/test_plan_followup.py`

**Interfaces — Produces :**
- `InstallmentDue(plan_id: int, number: int, amount: Decimal, due_date: date, stage: Stage, days_late: int)`
- `installment_steps(plan: Plan, done: dict[int, list[tuple[int, date]]], last_contact: date | None, today: date, delays: StageDelays = StageDelays()) -> list[InstallmentDue]` — mensualités non reçues échues ; palier suivant seulement si le précédent a été validé et le délai atteint ; délai minimal de 7 jours depuis `last_contact`
- `dispute_due(plan: Plan, done, today) -> bool` — une mensualité non reçue a 3 relances validées et la dernière date d'au moins 7 jours
- `refresh_plans(recovery, source, today) -> list[str]` (dans `plans.py`) — passe à *soldé* un plan dont toutes les mensualités sont reçues ou dont toutes les factures couvertes sont payées dans agent-factures (Review Focus 3) ; renvoie les messages pour l'interface
- `installment_reminder_draft(due: InstallmentDue, client: Client, company: str) -> ReminderDraft` — texte déterministe par palier (tons V1 : courtois, ferme, dernier rappel), objet avec `[Réf. E-NNNNN]`, vérifié par `check_plan_draft` (Task 8) sur ce seul montant et cette seule date

- [ ] **Step 1 : tests** — mensualité échue depuis 2 jours : rien ; 3 jours : palier 1 ; palier 2 seulement après validation du 1 et à J+15 ; délai de 7 jours ; mensualité reçue : rien ; contentieux proposé 7 jours après la 3e relance, pas avant ; factures couvertes toutes payées → plan soldé ; brouillon de relance de mensualité conforme au contrôle.
- [ ] **Steps 2-4**, **Step 5 :** commit `feat: suivi des mensualités et propositions de contentieux`.

---

### Task 8 : mails d'échéancier (rédaction et vérification)

**Files :** Create `src/agent_relances/drafting/plans.py` ; Modify `src/agent_relances/drafting/checker.py` ; Test `tests/test_plan_drafts.py`

**Interfaces — Produces :**
- `check_plan_draft(draft: ReminderDraft, amounts: list[Decimal], total: Decimal | None, dates: list[date] | None) -> list[str]` — montants trouvés = exactement `set(amounts) ∪ {total}` (si `total`) ; si `dates` : chaque date présente avec son mois, aucune autre ; aucun pourcentage ; sans `dates`, aucune date
- `proposal_draft(api, plan: Plan, client: Client, company: str, requested: int | None, model: str = WRITER_MODEL) -> tuple[ReminderDraft, DraftSource, list[str], Usage]` — Claude rédige depuis des faits structurés (même neutralisation que `writer.facts_block`) ; vérifié par `check_plan_draft(…, dates=None)` ; une nouvelle tentative avec les erreurs, puis texte de secours déterministe ; peut dire « vous nous demandiez N mois »
- `confirmation_draft(api, plan, client, company, model=WRITER_MODEL) -> tuple[ReminderDraft, DraftSource, list[str], Usage]` — idem avec les dates ; objet avec `[Réf. E-NNNNN]`
- `deposit_plan_draft(outbox: Path, plan: Plan, client: Client, draft: ReminderDraft) -> str` — revérifie `plan_violations` **et** `check_plan_draft` avant d'écrire le `.eml` ; lève `PlanRuleError` sinon

- [ ] **Step 1 : tests** — brouillon exact accepté ; montant faux, mensualité oubliée, 5e mensualité inventée, date fausse, pourcentage → refusés ; « vous nous demandiez 6 mois » accepté ; textes de secours toujours conformes (plans de 2, 3 et 4 mensualités, avec et sans dates) ; `deposit_plan_draft` refuse un plan modifié à la main hors règle.
- [ ] **Steps 2-4**, **Step 5 :** commit `feat: mails de proposition et de confirmation d'échéancier vérifiés par le code`.

---

### Task 9 : dossier de contentieux

**Files :** Create `src/agent_relances/disputes/__init__.py`, `disputes/dossier.py` ; Modify `sources.py` (`InvoiceSource.files_of(invoice_ids) -> dict[int, str]` : `source_file` d'agent-factures) ; Test `tests/test_dossier.py`

**Interfaces — Produces :**
- `DossierResult(folder: Path, pdf_path: Path, eml_path: Path, copied: list[str], missing: list[str], total_due: Decimal)`
- `build_dossier(client: Client, invoices: list[OverdueInvoice], files: dict[int, str], reminders: list[StoredReminder], replies: list, plan: Plan | None, out_root: Path, lawyer_email: str, company: str, today: date, base_dir: Path) -> DossierResult` — dossier `out_root/<client_key sans espaces>_<AAAA-MM-JJ>/` ; PDF reportlab « Dossier de recouvrement » (client et contact ; factures : numéro, date, montant TTC, échéance ; reste dû = total − mensualités reçues ; chronologie datée des relances validées avec objet ; réponses du client résumées ; échéancier avec mensualités reçues et manquées) ; copie des fichiers lisibles (chemins relatifs résolus depuis `base_dir`, le dossier d'agent-factures), liste des absents dans le PDF ; brouillon `.eml` à `lawyer_email` (« à compléter » si vide, sans en-tête To) ; jamais d'envoi
- `open_dispute(...)` (dans `disputes/dossier.py`) : construit le dossier, passe le plan à *rompu* le cas échéant, `add_hold(…, "suspension", None, "contentieux")`, `add_dispute`, journal

- [ ] **Step 1 : tests** — le PDF existe et son texte (via `pypdf`) contient chaque numéro de facture, le reste dû au format français, chaque date de relance ; fichier introuvable listé comme absent ; le `.eml` ne contient aucun montant absent du PDF ; après `open_dispute`, plus aucune relance ni relance de mensualité pour ce client.
- [ ] **Steps 2-4**, **Step 5 :** commit `feat: dossier de contentieux pour l'avocat`.

---

### Task 10 : interface (Réponses clients, Échéanciers, Contentieux, tableau de bord)

**Files :** Modify `src/agent_relances/web.py`, `src/agent_relances/metrics.py`, `app/main.py` ; Create `app/app_pages/reponses.py`, `echeanciers.py`, `contentieux.py` ; Test `tests/test_app.py`, `tests/test_metrics.py`

**Interfaces — Produces :** `web.render_replies_page()`, `render_plans_page()`, `render_disputes_page()` ; navigation : `Relances du jour · Réponses clients · Échéanciers · Clients · Tableau de bord · Journal ▾ (… · Contentieux)` ; clés : `relever-reponses`, `confirmer-<id>`, `classer-<id>`, `mensualites-<id>` (nombre imposé, défaut = proposition), `recu-<plan>-<n>`, `annuler-plan-<id>`, `contentieux-<client_key>` ; `Dashboard` gagne `plans_active`, `plans_remaining: Decimal`, `disputes`.

- [ ] **Step 1 : tests AppTest** — chaque nouvelle page s'affiche ; « Relever les réponses » sans clé d'API : les réponses rattachées sont lues mais non comprises, et marquées « à traiter par un humain » avec un message clair ; confirmer une promesse de date crée la pause ; imposer 5 mensualités affiche le refus avec la raison ; marquer une mensualité reçue met à jour le reste dû ; le bouton contentieux crée le dossier et affiche son chemin ; le corps des réponses s'affiche en `st.text` et le résumé via `md_escape` ; les relances validées affichent leur référence `R-NNNNN` dans le journal.
- [ ] **Steps 2-4** (+ démarrage de Streamlit sur un port libre, vérification sans trace d'erreur, arrêt du serveur), **Step 5 :** commit `feat: interface des réponses, échéanciers et contentieux`.

---

### Task 11 : démo et évaluation

**Files :** Modify `demo/seed.py` ; Create `evals/replies/__init__.py`, `evals/replies/generate.py`, `evals/replies/run.py`, `evals/replies/dataset/` ; Test `tests/test_replies_eval.py`, `tests/test_seed.py`

**Interfaces — Produces :**
- Démo : `seed()` écrit aussi `data/demo_inbox/` avec 6 réponses `.eml` (promesse de date, demande en 6 fois, contestation, « j'ai payé », réponse frauduleuse avec nouveau RIB, accord) portant les références des relances de la démo, un échéancier *en cours* avec une mensualité en retard depuis 40 jours et 3 relances validées (contentieux proposable), et `.env.example` pointe `AGENT_RELANCES_INBOX=data/demo_inbox` en commentaire de démo.
- Évaluation : `build_replies() -> list[dict]` — environ 40 réponses générées par le code, avec réponses attendues écrites d'après la spec (intention, date attendue ou null, nombre de mois ou null, client attendu, `fraud`) ; cas pièges : réponse polie mais contestataire, échéancier demandé en 6 fois, date en toutes lettres, « fin du mois », réponse en anglais, autre adresse que la fiche avec référence, réponse frauduleuse avec nouveau RIB, accord sans proposition.
- `evaluate_replies(replies, api, model, company, today, max_cost=None) -> dict` et CLI `uv run python -m evals.replies.run [--model M] [--max-cost X]` : intention correcte, date correcte, mois corrects, rattachement correct, fraudes bloquées, coût moyen ; résultats partiels écrits même en cas de plantage (comme la V1).
- Le test exhaustif de la règle (Task 1) reste le critère « 100 % conformes ».

- [ ] **Step 1 : tests** — composition du jeu (≈ 40, chaque intention ≥ 5 cas, ≥ 3 fraudes) ; `evaluate_replies` avec un faux client parfait donne 100 % et un faux client qui se trompe une fois donne le bon pourcentage ; la démo produit les 6 réponses rattachées à la bonne fiche ; budget : arrêt dès le plafond.
- [ ] **Steps 2-4**, **Step 5 :** commit `feat: démonstration et évaluation des réponses clients`.
- [ ] **Step 6 (contrôleur, avec accord de l'utilisateur seulement) :** `uv run python -m evals.replies.run --max-cost 1` puis `--model claude-sonnet-5 --max-cost 1`.

---

### Task 12 : agent-emails reconnaît les réponses aux relances ; README

**Files :** dans **agent-emails** : Modify `src/agent_emails/pipeline/runner.py`, Test `tests/test_runner.py` ; dans **agent-relances** : Modify `README.md`

**Interfaces — Produces (agent-emails) :** `RELANCE_REFERENCE = re.compile(r"\[Réf\. [RE]-\d{5}\]")` ; dans `Pipeline.process`, avant tout appel au modèle : objet porteur d'une référence → `EmailResult` au statut `a_ignorer`, sans brouillon, `error=None`, triage absent, et une explication « Réponse à une relance : traitée par agent-relances » enregistrée dans le journal ; aucun coût.

- [ ] **Step 1 : test (agent-emails)** — un email `Re: Rappel facture AL-1 [Réf. R-00042]` n'appelle pas le modèle (`FakeClient([])`) et finit `a_ignorer` ; un email sans référence suit le pipeline normal.
- [ ] **Steps 2-4** dans agent-emails (sa suite complète verte), commit `feat: les réponses aux relances sont laissées à agent-relances` dans agent-emails (sans pousser).
- [ ] **Step 5 : README d'agent-relances** — section « Réponses, échéanciers et contentieux » : la règle d'échéancier, le cycle de vie, la boucle avec agent-emails, le dossier pour l'avocat ; résultats chiffrés après le run d'évaluation (sinon phrase d'attente) ; commit `docs: README de la V2`.
