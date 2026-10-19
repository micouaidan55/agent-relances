"""Éléments d'interface Streamlit partagés par les pages de app/."""

import logging
import os
import re
from datetime import date
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st
from agent_factures.agent.loop import DEFAULT_COMPANY
from dotenv import load_dotenv

from agent_relances.disputes.dossier import open_dispute
from agent_relances.drafting.checker import check_plan_draft
from agent_relances.formatting import date_fr, euros
from agent_relances.metrics import dashboard
from agent_relances.models import (
    STAGE_LABELS, ActionKind, Client, DraftSource, Plan, PlanStatus, Profile, ReminderStatus, ReplyIntent,
    ReplyStatus, Tone, client_key,
)
from agent_relances.outbox import reference, save_plan_eml
from agent_relances.plans import cancel_plan, installment_reminder_draft, merged_last_contacts, refresh_plans
from agent_relances.prepare import prepare_today, validate_reminder
from agent_relances.replies.process import (
    attach_reply, confirm_reply, dismiss_reply, process_replies, record_agreement,
)
from agent_relances.rules.installments import InstallmentDue, dispute_due, installment_steps, max_installments
from agent_relances.rules.profile import client_profile
from agent_relances.rules.schedule import UNNAMED_CLIENT
from agent_relances.sources import FacturesSourceError, InvoiceSource
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import DISPUTE_HOLD_REASON, RecoveryStore
from agent_relances.storage.repository import ReminderStore, StoredReminder

load_dotenv()
log = logging.getLogger(__name__)

PROFILE_LABELS = {Profile.GOOD: "Bon payeur", Profile.OCCASIONAL: "Occasionnel", Profile.CHRONIC: "Retardataire chronique"}
TONE_LABELS = {Tone.LIGHT: "Léger", Tone.NEUTRAL: "Neutre", Tone.FIRM: "Ferme"}
STATUS_LABELS = {
    ReminderStatus.TO_VALIDATE: "À valider", ReminderStatus.VALIDATED: "Validée", ReminderStatus.POSTPONED: "Reportée",
    ReminderStatus.HUMAN: "À traiter par un humain", ReminderStatus.CANCELLED: "Annulée",
}
INTENT_LABELS = {
    ReplyIntent.PAYMENT_ANNOUNCED: "Paiement annoncé", ReplyIntent.PROMISED_DATE: "Date de paiement promise",
    ReplyIntent.PLAN_REQUEST: "Demande d'échéancier", ReplyIntent.PLAN_AGREEMENT: "Accord sur l'échéancier",
    ReplyIntent.DISPUTE: "Contestation", ReplyIntent.OTHER: "Autre",
}
ACTION_LABELS = {
    ActionKind.POSTPONE: "Reporter les relances", ActionKind.SUSPEND: "Suspendre les relances",
    ActionKind.PROPOSE_PLAN: "Proposer un échéancier", ActionKind.ACTIVATE_PLAN: "Activer l'échéancier",
    ActionKind.HUMAN: "À traiter par un humain", ActionKind.NONE: "Sans objet",
}
REPLY_STATUS_LABELS = {
    ReplyStatus.TO_REVIEW: "À traiter", ReplyStatus.APPLIED: "Appliquée", ReplyStatus.DISMISSED: "Classée",
    ReplyStatus.UNMATCHED: "Non rattachée",
}
PLAN_STATUS_LABELS = {
    PlanStatus.PROPOSED: "Proposé", PlanStatus.ACTIVE: "En cours", PlanStatus.SETTLED: "Soldé",
    PlanStatus.BROKEN: "Rompu", PlanStatus.CANCELLED: "Annulé",
}
NO_API_KEY_REPLIES = "Clé d'API absente : réponses enregistrées sans lecture automatique, à traiter par un humain."
NEW_CARD = "Nouvelle fiche"
SOURCE_LABELS = {DraftSource.MODEL: "Rédigée par Claude, faits vérifiés", DraftSource.FALLBACK: "Modèle de texte de secours"}

_MD_SPECIAL = set("\\`*_{}[]()#+-.!|<>:~$&")
_AUTOLINK = re.compile(r"(@|://|www(?=\\\.))", re.IGNORECASE)


def md_escape(text: str | None) -> str:
    """Neutralise le Markdown d'un texte saisi, issu d'une facture ou du modèle (et empêche les liens automatiques)."""
    escaped = "".join(f"\\{c}" if c in _MD_SPECIAL else c for c in (text or ""))
    return _AUTOLINK.sub(lambda m: m.group(1)[0] + "⁠" + m.group(1)[1:], escaped)


def today() -> date:
    value = os.environ.get("TODAY", "").strip()
    try:
        return date.fromisoformat(value) if value else date.today()
    except ValueError:
        return date.today()


def company() -> str:
    return os.environ.get("COMPANY_NAME", DEFAULT_COMPANY)


def _path(variable: str, default: str) -> Path:
    return Path(os.environ.get(variable, default))


@st.cache_resource
def _connection(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return connect(path)


def store() -> ReminderStore:
    return ReminderStore(_connection(str(_path("AGENT_RELANCES_DB", "data/relances.db"))))


def invoice_source() -> InvoiceSource:
    return InvoiceSource.open_readonly(_path("AGENT_FACTURES_DB", "data/demo_factures.db"))


def outbox() -> Path:
    return _path("AGENT_RELANCES_OUTBOX", "data/relances_deposees")


def recovery() -> RecoveryStore:
    return RecoveryStore(_connection(str(_path("AGENT_RELANCES_DB", "data/relances.db"))))


def inbox() -> Path:
    return _path("AGENT_RELANCES_INBOX", "../agent-emails/inbox")


def dossiers_root() -> Path:
    return _path("AGENT_RELANCES_DOSSIERS", "data/contentieux")


def company_domain() -> str:
    return os.environ.get("COMPANY_DOMAIN", "").strip()


def lawyer_email() -> str:
    return os.environ.get("AVOCAT_EMAIL", "").strip()


def factures_base_dir() -> Path:
    """Racine d'agent-factures : ses `source_file` sont relatifs à sa racine (data/uploads/…, inbox/…) et sa base
    est dans `<racine>/data/`."""
    return _path("AGENT_FACTURES_DB", "data/demo_factures.db").resolve().parent.parent


def api_client() -> anthropic.Anthropic | None:
    return anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None


def _source_or_error() -> InvoiceSource | None:
    try:
        return invoice_source()
    except FacturesSourceError as exc:
        st.error(f"{md_escape(str(exc))}. Vérifie AGENT\\_FACTURES\\_DB dans le fichier .env, "
                 "ou crée la base de démonstration : uv run python \\-m demo.seed")
        return None


def _flash() -> None:
    message = st.session_state.pop("flash", None)
    if message:
        kind, text, *code = message
        getattr(st, kind)(text)
        if code:
            st.code(code[0], language=None, wrap_lines=True)


def _render_reminder(reminder: StoredReminder, reminders: ReminderStore, source: InvoiceSource) -> None:
    key = reminder.id
    with st.container(border=True):
        st.subheader(f"{md_escape(reminder.client_name)} — {euros(reminder.total)}")
        st.caption(" · ".join([
            f"Palier {int(reminder.stage)} : {STAGE_LABELS[reminder.stage]}",
            f"Profil : {PROFILE_LABELS.get(reminder.profile, '—')}",
            f"Ton : {TONE_LABELS.get(reminder.tone, '—')}",
            SOURCE_LABELS[reminder.source],
        ]))
        st.dataframe(pd.DataFrame([{"Facture": i.number, "Montant TTC": euros(i.amount), "Échéance": date_fr(i.due_date),
                                    "Retard": f"{i.days_late} j"} for i in reminder.invoices]), hide_index=True)
        if reminder.source is DraftSource.FALLBACK:
            st.info("Modèle de texte de secours utilisé : relis le texte avant de valider.")
            if reminder.problems:
                with st.expander("Pourquoi ?"):
                    for problem in reminder.problems:
                        st.write(md_escape(problem))
        subject = st.text_input("Objet", reminder.subject, key=f"objet-{key}")
        body = st.text_area("Relance (jamais envoyée automatiquement)", reminder.body, height=300, key=f"corps-{key}")
        actions = st.container(horizontal=True, vertical_alignment="bottom")
        if actions.button("Valider et déposer", type="primary", key=f"valider-{key}"):
            result = validate_reminder(reminders, source, key, subject, body, outbox(), today())
            if result.ok:
                st.session_state["flash"] = ("success", f"Relance déposée pour {md_escape(reminder.client_name)}.")
                st.rerun()
            elif result.cancelled:
                st.session_state["flash"] = ("warning", md_escape(result.problems[0]))
                st.rerun()
            else:
                st.error("Dépôt refusé : " + " ; ".join(md_escape(p) for p in result.problems))
        days = actions.number_input("Reporter de (jours)", min_value=1, max_value=90, value=7, key=f"jours-{key}")
        if actions.button("Reporter", key=f"reporter-{key}"):
            if reminders.postpone(key, int(days), today()):
                reminders.log(key, "reportee", {"jours": int(days)})
            st.rerun()
        if actions.button("Passer à un humain", key=f"humain-{key}"):
            if reminders.to_human(key, today()):
                reminders.log(key, "passage_humain", {})
            st.rerun()
        st.caption(f"Coût de la rédaction : {reminder.cost_usd:.4f} $ · {reminder.seconds:.1f} s")


def render_today_page() -> None:
    st.caption(f"Date du jour : {date_fr(today())}")
    _flash()
    source = _source_or_error()
    if source is None:
        return
    reminders = store()
    holds_and_plans = recovery()
    api = api_client()
    if api is None:
        st.warning("Clé d'API Claude absente : les relances seront préparées avec le modèle de texte de secours.")
    if st.button("Préparer les relances du jour", type="primary", icon=":material/mail:"):
        with st.status("Préparation des relances…", expanded=True) as box:
            try:
                result = prepare_today(
                    source, reminders, api, company(), today(), recovery=holds_and_plans,
                    on_progress=lambda p: box.write(
                        f"{md_escape(p.due.client.name)} : palier {int(p.due.stage)}, {SOURCE_LABELS[p.source]}"))
            except Exception:
                box.update(label="La préparation a échoué", state="error")
                st.error("La préparation des relances a échoué. Réessaie dans un instant.")
            else:
                box.update(label=f"{len(result.prepared)} relance(s) préparée(s)", state="complete", expanded=False)
                st.session_state["last_preparation"] = (result.missing_clients, [i.number for i in result.escalated])
    missing, escalated = st.session_state.get("last_preparation", ([], []))
    if missing:
        st.warning("Fiches clients à compléter (aucune relance préparée) : "
                   + ", ".join(md_escape(name) for name in missing) + ". Complète\\-les dans la page Clients.")
    if escalated:
        st.info("Passées à un humain après le dernier palier : " + ", ".join(md_escape(n) for n in escalated) + ".")
    pending = sorted(reminders.list_reminders({ReminderStatus.TO_VALIDATE}), key=lambda r: r.total, reverse=True)
    st.caption(f"{len(pending)} relance(s) à valider")
    if not pending:
        st.info("Aucune relance à valider. Clique sur « Préparer les relances du jour ».")
    for reminder in pending:
        _render_reminder(reminder, reminders, source)


def _fill_card(client: Client | None) -> None:
    """Préremplit le formulaire de fiche, ou le vide : à appeler avant la création des champs (ou en rappel)."""
    name, email, contact, notes, professional = (
        (client.name, client.email, client.contact, client.notes, client.professional) if client
        else ("", "", "", "", True))
    st.session_state.update({"fiche-nom": name, "fiche-email": email, "fiche-contact": contact,
                             "fiche-notes": notes, "fiche-pro": professional})
    if client is None:
        st.session_state["fiche-choix"] = NEW_CARD


def render_clients_page() -> None:
    reminders = store()
    source = _source_or_error()
    clients = reminders.list_clients()
    by_key = {c.key: c for c in clients}
    overdue = source.overdue(today()) if source else []
    history = source.payment_history() if source else {}
    missing = sorted({i.customer or UNNAMED_CLIENT for i in overdue
                      if client_key(i.customer) not in by_key or not by_key[client_key(i.customer)].complete})
    if missing:
        st.warning("Clients avec des factures en retard et une fiche à compléter : "
                   + ", ".join(md_escape(name) for name in missing))
    _flash()
    if st.session_state.pop("fiche-reset", False):
        _fill_card(None)
    by_name = {c.name: c for c in clients}
    st.selectbox("Modifier une fiche existante", [NEW_CARD, *by_name], key="fiche-choix",
                 on_change=lambda: _fill_card(by_name.get(st.session_state["fiche-choix"])))
    st.session_state.setdefault("fiche-pro", True)
    with st.form("fiche"):
        st.subheader("Créer ou modifier une fiche")
        name = st.text_input("Nom du client, tel qu'il figure sur les factures", key="fiche-nom")
        email = st.text_input("Email", key="fiche-email")
        contact = st.text_input("Nom du contact", key="fiche-contact")
        notes = st.text_area("Notes (servent au ton, jamais citées)", key="fiche-notes")
        professional = st.checkbox("Client professionnel", key="fiche-pro")
        if st.form_submit_button("Enregistrer la fiche"):
            if not name.strip():
                st.error("Le nom du client est obligatoire.")
            elif email.strip() and "@" not in email:
                st.error("Adresse email invalide.")
            else:
                reminders.save_client(Client(name=name, email=email, contact=contact, notes=notes,
                                             professional=professional))
                reminders.log(None, "fiche_enregistree", {"client": name.strip()})
                st.session_state["flash"] = ("success", f"Fiche enregistrée : {md_escape(name.strip())}")
                st.session_state["fiche-reset"] = True
                st.rerun()
    if not clients:
        st.info("Aucune fiche client pour l'instant.")
        return
    counts: dict[str, int] = {}
    for invoice in overdue:
        counts[client_key(invoice.customer)] = counts.get(client_key(invoice.customer), 0) + 1
    validated: dict[str, int] = {}
    for reminder in reminders.list_reminders({ReminderStatus.VALIDATED}):
        validated[reminder.client_key] = validated.get(reminder.client_key, 0) + 1
    st.dataframe(pd.DataFrame([{
        "Client": c.name, "Email": c.email or "à compléter", "Contact": c.contact,
        "Type": "Professionnel" if c.professional else "Particulier",
        "Profil": PROFILE_LABELS[client_profile(history.get(c.key, []), counts.get(c.key, 0), today())],
        "Factures en retard": counts.get(c.key, 0),
        "Relances validées": validated.get(c.key, 0),
    } for c in clients]), hide_index=True)
    _render_holds(reminders, recovery())
    history_by_client: dict[str, list[StoredReminder]] = {}
    for reminder in reminders.list_reminders():
        history_by_client.setdefault(reminder.client_key, []).append(reminder)
    for c in clients:
        items = history_by_client.get(c.key, [])
        with st.expander(f"Historique des relances — {md_escape(c.name)} ({len(items)})"):
            if not items:
                st.caption("Aucune relance pour ce client.")
            for reminder in items:
                when = date_fr(reminder.decided_on or reminder.prepared_on)
                numbers = ", ".join(i.number for i in reminder.invoices)
                st.markdown(f"{when} · palier {int(reminder.stage)} · {STATUS_LABELS[reminder.status]} · "
                            f"{md_escape(numbers)}")


def _render_holds(reminders: ReminderStore, recovery_: RecoveryStore) -> None:
    """Pauses et suspensions en cours ; l'humain peut les lever, sauf la suspension d'un contentieux."""
    holds = recovery_.list_active_holds(today())
    if not holds:
        return
    st.subheader("Relances en pause")
    for hold in holds:
        when = f"reportées jusqu'au {date_fr(hold['until'])}" if hold["until"] else "suspendues sans date de fin"
        row = st.container(horizontal=True, vertical_alignment="center")
        row.markdown(f"**{md_escape(_client_name(reminders, hold['client_key']))}** : relances {when} · "
                     f"{md_escape(hold['reason'])}")
        if hold["reason"] == DISPUTE_HOLD_REASON:
            row.caption("Client en contentieux : cette suspension ne se lève pas ici.")
        elif row.button("Lever la pause", key=f"lever-pause-{hold['id']}"):
            if recovery_.release_hold(hold["id"], today()):
                reminders.log(None, "pause_levee", {"pause": hold["id"], "client": hold["client_key"]})
                st.session_state["flash"] = ("success", f"Pause levée : les relances reprennent pour "
                                                        f"{md_escape(_client_name(reminders, hold['client_key']))}.")
            st.rerun()


def render_dashboard_page() -> None:
    source = _source_or_error()
    if source is None:
        return
    board = dashboard(source, store(), today(), recovery())
    row = st.container(horizontal=True)
    row.metric("Montant en retard", euros(board.overdue_total))
    row.metric("Factures en retard", board.overdue_count)
    row.metric("Relances à valider", board.pending)
    row.metric("Chez un humain", board.to_humans)
    row = st.container(horizontal=True)
    row.metric("Relances validées", board.validated)
    row.metric("Encaissé après relance", euros(board.recovered_total), help=f"{board.recovered_count} facture(s)")
    row.metric("Délai moyen relance → paiement",
               f"{board.mean_days_to_payment:.0f} j" if board.mean_days_to_payment is not None else "—")
    row.metric("Coût moyen par relance", f"{board.mean_cost_usd:.4f} $")
    row.metric("Recours au modèle de secours",
               f"{board.fallback_rate:.0%}" if board.fallback_rate is not None else "—")
    row = st.container(horizontal=True)
    row.metric("Échéanciers en cours", board.plans_active)
    row.metric("Reste dû sur échéanciers", euros(board.plans_remaining))
    row.metric("Contentieux", board.disputes)
    st.subheader("Factures en retard par palier")
    st.bar_chart(pd.DataFrame({"Palier": list(board.by_stage), "Factures": list(board.by_stage.values())}),
                 x="Palier", y="Factures")


def _render_list(status: ReminderStatus, empty: str) -> None:
    items = store().list_reminders({status})
    if not items:
        st.info(empty)
    for reminder in items:
        when = date_fr(reminder.decided_on) if reminder.decided_on else "—"
        numbers = ", ".join(i.number for i in reminder.invoices)
        ref = reference("R", reminder.id)
        label = f"{md_escape(reminder.client_name)} — {md_escape(numbers)} — {when}"
        with st.expander(f"{label} — {ref}" if status is ReminderStatus.VALIDATED else label):
            if status is ReminderStatus.VALIDATED:
                st.caption(f"Référence : {ref}")
            if reminder.body:
                st.code(reminder.body, language=None, wrap_lines=True)
            if reminder.eml_path:
                st.caption(f"Déposée : {md_escape(reminder.eml_path)}")


def render_validated_page() -> None:
    _render_list(ReminderStatus.VALIDATED, "Aucune relance validée pour l'instant.")


def render_humans_page() -> None:
    _flash()
    _render_list(ReminderStatus.HUMAN, "Aucune facture à traiter par un humain.")
    source = _source_or_error()
    if source is not None:
        _render_dispute_opener(store(), recovery(), source)


def _render_dispute_opener(reminders: ReminderStore, recovery_: RecoveryStore, source: InvoiceSource) -> None:
    """Ouverture d'un contentieux à la main, pour un client qui a des factures impayées et aucun contentieux."""
    with_dispute = {d["client_key"] for d in recovery_.list_disputes()}
    candidates = {c.name: c.key for c in reminders.list_clients()
                  if c.key not in with_dispute and source.unpaid_of(c.key, today())}
    st.subheader("Ouvrir un contentieux")
    if not candidates:
        st.caption("Aucun client avec des factures impayées hors contentieux.")
        return
    row = st.container(horizontal=True, vertical_alignment="bottom")
    name = row.selectbox("Client", list(candidates), key="contentieux-client")
    if row.button("Ouvrir un contentieux", key="ouvrir-contentieux",
                  help="Prépare le dossier pour l'avocat (jamais envoyé) et suspend les relances de ce client."):
        _open_dispute(candidates[name], reminders, recovery_, source)


def render_history_page() -> None:
    entries = store().list_log()
    if not entries:
        st.info("Le journal est vide.")
        return
    st.dataframe(pd.DataFrame(entries), hide_index=True)


# --- Réponses des clients ---


def _client_name(reminders: ReminderStore, key: str | None) -> str:
    card = reminders.get_client(key) if key else None
    return card.name if card else (key or "Client inconnu")


def _default_installments(total, requested: int | None) -> int:
    maximum = max_installments(total) if total else 2
    return max(1, min(requested if requested and requested >= 2 else maximum, maximum))


def _render_reply(reply: dict, reminders: ReminderStore, recovery_: RecoveryStore, source: InvoiceSource,
                  api) -> None:
    reply_id, kind = reply["id"], reply["proposed_action"]
    with st.container(border=True):
        st.subheader(f"{md_escape(_client_name(reminders, reply['client_key']))} — {INTENT_LABELS[reply['intent']]}")
        st.caption(f"Reçue le {date_fr(date.fromisoformat(reply['received_at'][:10]))} · "
                   f"de {md_escape(reply['sender'])} · objet : {md_escape(reply['subject'])}")
        st.markdown(md_escape(reply["summary"]))
        if reply["suspicious"]:
            st.warning("Réponse suspecte (fraude possible) : vérifie l'expéditeur avant toute action.")
        detail = f" jusqu'au {date_fr(reply['action_until'])}" if reply["action_until"] else ""
        st.info(f"Action proposée : {ACTION_LABELS[kind]}{detail}")
        with st.expander("Lire le message"):
            st.text(reply["body"])
        actions = st.container(horizontal=True, vertical_alignment="bottom")
        count = None
        if kind is ActionKind.PROPOSE_PLAN:
            total = sum((i.amount for i in source.unpaid_of(reply["client_key"], today())), start=0)
            count = actions.number_input(
                "Nombre de mensualités", min_value=1, max_value=12, key=f"mensualites-{reply_id}",
                value=_default_installments(total, reply["requested_installments"]),
                help="4 mensualités au maximum, au moins 100 € chacune ; 2 fois si le total est inférieur à 200 €.")
        if actions.button("Confirmer l'action", type="primary", key=f"confirmer-{reply_id}"):
            errors = confirm_reply(reply_id, reminders, recovery_, source, outbox(), api, company(), today(),
                                   installments=int(count) if count is not None else None)
            if errors:
                st.error("Action refusée : " + " ; ".join(md_escape(e) for e in errors))
            else:
                st.session_state["flash"] = ("success", f"Action appliquée : {ACTION_LABELS[kind]}.")
                st.rerun()
        if actions.button("Classer sans action", key=f"classer-{reply_id}"):
            if dismiss_reply(reply_id, recovery_, today()):
                reminders.log(None, "reponse_classee", {"reponse": reply_id})
            st.rerun()


def _render_unmatched(reply: dict, reminders: ReminderStore, recovery_: RecoveryStore, source: InvoiceSource,
                      api) -> None:
    with st.container(border=True):
        st.markdown(f"**{md_escape(reply['sender'])}** · objet : {md_escape(reply['subject'])}")
        st.caption(f"Reçue le {date_fr(date.fromisoformat(reply['received_at'][:10]))} · non rattachée à un client")
        if reply["suspicious"]:
            st.warning("Réponse suspecte (fraude possible) : vérifie l'expéditeur avant toute action.",
                       icon=":material/warning:")
        with st.expander("Lire le message"):
            st.text(reply["body"])
        cards = {c.name: c.key for c in reminders.list_clients()}
        if not cards:
            st.caption("Aucune fiche client : crée la fiche dans la page Clients pour rattacher cette réponse.")
            return
        row = st.container(horizontal=True, vertical_alignment="bottom")
        name = row.selectbox("Rattacher à…", list(cards), key=f"rattacher-client-{reply['id']}")
        if row.button("Rattacher", key=f"rattacher-{reply['id']}",
                      help="La réponse est relue pour ce client et reçoit une action proposée, à confirmer."):
            errors = attach_reply(reply["id"], cards[name], reminders, recovery_, source, api, company(),
                                  company_domain(), today())
            if errors:
                st.error("Rattachement refusé : " + " ; ".join(md_escape(e) for e in errors))
            else:
                st.session_state["flash"] = ("success", f"Réponse rattachée à {md_escape(name)} : à traiter.")
                st.rerun()


def render_replies_page() -> None:
    _flash()
    source = _source_or_error()
    if source is None:
        return
    reminders, recovery_, api = store(), recovery(), api_client()
    if api is None:
        st.warning(NO_API_KEY_REPLIES)
    if st.button("Relever les réponses", type="primary", icon=":material/inbox:", key="relever-reponses"):
        folder = inbox()
        if not folder.is_dir():
            st.error(f"Dossier de réception introuvable : {md_escape(str(folder))}. "
                     "Vérifie AGENT\\_RELANCES\\_INBOX dans le fichier .env.")
        else:
            try:
                ids = process_replies(folder, reminders, recovery_, source, api, company(), company_domain(), today())
            except Exception:
                log.exception("Relevé des réponses échoué")
                st.error("Le relevé des réponses a échoué. Réessaie dans un instant.")
            else:
                st.success(f"{len(ids)} nouvelle(s) réponse(s) enregistrée(s).")
    replies = recovery_.list_replies()
    to_review = [r for r in replies if r["status"] is ReplyStatus.TO_REVIEW]
    st.caption(f"{len(to_review)} réponse(s) à traiter")
    if not to_review:
        st.info("Aucune réponse à traiter. Clique sur « Relever les réponses ».")
    for reply in to_review:
        _render_reply(reply, reminders, recovery_, source, api)
    unmatched = [r for r in replies if r["status"] is ReplyStatus.UNMATCHED]
    if unmatched:
        st.subheader(f"Non rattachées ({len(unmatched)})")
    for reply in unmatched:
        _render_unmatched(reply, reminders, recovery_, source, api)
    others = [r for r in replies if r["status"] in (ReplyStatus.APPLIED, ReplyStatus.DISMISSED)]
    if others:
        with st.expander(f"Réponses déjà traitées ({len(others)})"):
            for reply in others:
                st.markdown(f"{date_fr(reply['created_on'])} · {REPLY_STATUS_LABELS[reply['status']]} · "
                            f"{md_escape(_client_name(reminders, reply['client_key']))} · {md_escape(reply['summary'])}")
                st.text(reply["body"])


# --- Échéanciers ---


def _due_installments(reminders: ReminderStore, recovery_: RecoveryStore, on: date) -> list[tuple[Plan, InstallmentDue]]:
    """Relances de mensualités dues : une seule par client (la plus ancienne), hors clients en pause."""
    holds = recovery_.active_holds(on)
    last_contacts = merged_last_contacts(reminders, recovery_)
    result = []
    for plan in recovery_.list_plans([PlanStatus.ACTIVE]):
        if plan.client_key in holds:
            continue
        dues = installment_steps(plan, recovery_.installment_reminders(plan.id), last_contacts.get(plan.client_key),
                                 on)
        if dues:
            result.append((plan, min(dues, key=lambda d: d.number)))
    return result


def _dispute_proposals(recovery_: RecoveryStore, on: date) -> list[Plan]:
    with_dispute = {d["client_key"] for d in recovery_.list_disputes()}
    return [plan for plan in recovery_.list_plans([PlanStatus.ACTIVE])
            if plan.client_key not in with_dispute and dispute_due(plan, recovery_.installment_reminders(plan.id), on)]


def _installment_state(plan: Plan, installment) -> str:
    if installment.received_on is not None:
        return f"Reçue le {date_fr(installment.received_on)}"
    if plan.status is not PlanStatus.ACTIVE or installment.due_date is None:
        return "En attente d'accord" if plan.status is PlanStatus.PROPOSED else "—"
    return f"En retard ({(today() - installment.due_date).days} j)" if installment.due_date < today() else "À venir"


def _render_plan(plan: Plan, reminders: ReminderStore, recovery_: RecoveryStore, source: InvoiceSource) -> None:
    with st.container(border=True):
        st.subheader(f"{md_escape(_client_name(reminders, plan.client_key))} — échéancier "
                     f"{reference('E', plan.id)}")
        st.caption(f"{PLAN_STATUS_LABELS[plan.status]} · proposé le {date_fr(plan.proposed_on)} · "
                   f"total {euros(plan.total)}")
        st.metric("Reste dû", euros(plan.remaining))
        st.dataframe(pd.DataFrame([{
            "Mensualité": i.number, "Montant": euros(i.amount),
            "Échéance": date_fr(i.due_date) if i.due_date else "à fixer", "Situation": _installment_state(plan, i),
        } for i in plan.installments]), hide_index=True)
        if plan.status not in (PlanStatus.PROPOSED, PlanStatus.ACTIVE):
            return
        actions = st.container(horizontal=True)
        if plan.status is PlanStatus.PROPOSED and actions.button(
                "Accord du client reçu", type="primary", key=f"accord-plan-{plan.id}",
                help="Accord donné hors réponse écrite (téléphone…) : les dates partent d'aujourd'hui et la "
                     "confirmation est déposée."):
            errors = record_agreement(plan.id, reminders, recovery_, source, outbox(), api_client(), company(),
                                      today())
            if errors:
                st.error("Accord refusé : " + " ; ".join(md_escape(e) for e in errors))
            else:
                st.session_state["flash"] = ("success", "Échéancier activé : confirmation déposée.")
                st.rerun()
        if plan.status is PlanStatus.ACTIVE:
            for installment in plan.installments:
                if installment.received_on is None and actions.button(
                        f"Mensualité {installment.number} reçue", key=f"recu-{plan.id}-{installment.number}"):
                    if recovery_.mark_received(plan.id, installment.number, today()):
                        reminders.log(None, "mensualite_recue", {"echeancier": plan.id, "mensualite": installment.number})
                    closed = refresh_plans(recovery_, source, today())
                    if closed:
                        st.session_state["flash"] = ("success", " ".join(md_escape(m) for m in closed))
                    st.rerun()
        if actions.button("Annuler l'échéancier", key=f"annuler-plan-{plan.id}"):
            errors = cancel_plan(recovery_, plan.id, today())
            if errors:
                st.error(" ; ".join(md_escape(e) for e in errors))
            else:
                reminders.log(None, "echeancier_annule", {"echeancier": plan.id})
                st.rerun()


def _validate_installment_reminder(plan: Plan, due: InstallmentDue, client: Client, reminders: ReminderStore,
                                   recovery_: RecoveryStore) -> None:
    current = {(p.id, d.number, d.stage) for p, d in _due_installments(reminders, recovery_, today())}
    if (plan.id, due.number, int(due.stage)) not in current:
        st.session_state["flash"] = ("warning", "Cette relance n'est plus due (échéancier modifié, client en pause "
                                                "ou délai non atteint).")
        st.rerun()
    draft = installment_reminder_draft(due, client, company())
    problems = check_plan_draft(draft, [due.amount], None, [due.due_date], require_count=False)
    if problems:
        st.error("Dépôt refusé : " + " ; ".join(md_escape(p) for p in problems))
        return
    path = save_plan_eml(outbox(), plan.id, client, draft.subject, draft.body,
                         f"_mensualite-{due.number}-palier-{int(due.stage)}")
    if recovery_.record_installment_reminder(plan.id, due.number, int(due.stage), today(), path):
        reminders.log(None, "relance_mensualite_validee", {"echeancier": plan.id, "mensualite": due.number,
                                                           "palier": int(due.stage), "fichier": path})
        st.session_state["flash"] = ("success", f"Relance de la mensualité {due.number} déposée pour "
                                                f"{md_escape(client.name)}.")
    else:
        st.session_state["flash"] = ("warning", "Ce palier de relance était déjà enregistré.")
    st.rerun()


def _render_due_installments(reminders: ReminderStore, recovery_: RecoveryStore) -> None:
    due_list = _due_installments(reminders, recovery_, today())
    st.subheader("Relances de mensualités à valider")
    if not due_list:
        st.info("Aucune relance de mensualité due aujourd'hui.")
    for plan, due in due_list:
        client = reminders.get_client(plan.client_key)
        with st.container(border=True):
            st.markdown(f"**{md_escape(_client_name(reminders, plan.client_key))}** — mensualité {due.number} de "
                        f"{euros(due.amount)}, échue le {date_fr(due.due_date)} ({due.days_late} j de retard) · "
                        f"palier {int(due.stage)} : {STAGE_LABELS[due.stage]}")
            if client is None or not client.complete:
                st.warning("Fiche client incomplète : ajoute un email dans la page Clients avant de valider.")
                continue
            draft = installment_reminder_draft(due, client, company())
            st.text(draft.subject)
            st.text(draft.body)
            if st.button("Valider et déposer", type="primary",
                         key=f"valider-mensualite-{plan.id}-{due.number}-{int(due.stage)}"):
                _validate_installment_reminder(plan, due, client, reminders, recovery_)


def _open_dispute(key: str, reminders: ReminderStore, recovery_: RecoveryStore, source: InvoiceSource) -> None:
    card = reminders.get_client(key)
    if card is None:
        st.error("Fiche client introuvable.")
        return
    try:
        result = open_dispute(card, recovery_, reminders, source, dossiers_root(), lawyer_email(), company(), today(),
                              factures_base_dir())
    except ValueError as error:
        st.error(md_escape(str(error)))
        return
    except Exception:
        log.exception("Ouverture du contentieux échouée")
        st.error("Le dossier de contentieux n'a pas pu être créé.")
        return
    text = f"Dossier de contentieux ouvert pour {md_escape(card.name)}."
    if result.missing:
        text += " Fichiers de factures absents : " + ", ".join(md_escape(m) for m in result.missing) + "."
    st.session_state["flash"] = ("success", text, str(result.folder))
    st.rerun()


def _render_dispute_proposals(reminders: ReminderStore, recovery_: RecoveryStore, source: InvoiceSource) -> None:
    for plan in _dispute_proposals(recovery_, today()):
        name = _client_name(reminders, plan.client_key)
        with st.container(border=True):
            st.warning(f"Contentieux proposé pour {md_escape(name)} : trois relances de mensualité validées, "
                       "sans paiement depuis au moins 7 jours.")
            if st.button("Ouvrir le dossier de contentieux", key=f"contentieux-{plan.client_key}"):
                _open_dispute(plan.client_key, reminders, recovery_, source)


def render_plans_page() -> None:
    _flash()
    source = _source_or_error()
    if source is None:
        return
    reminders, recovery_ = store(), recovery()
    for message in refresh_plans(recovery_, source, today()):
        st.success(md_escape(message))
    _render_dispute_proposals(reminders, recovery_, source)
    _render_due_installments(reminders, recovery_)
    plans = recovery_.list_plans()
    st.subheader("Échéanciers")
    if not plans:
        st.info("Aucun échéancier pour l'instant.")
    for plan in plans:
        _render_plan(plan, reminders, recovery_, source)


# --- Contentieux ---


def render_disputes_page() -> None:
    _flash()
    source = _source_or_error()
    if source is None:
        return
    reminders, recovery_ = store(), recovery()
    _render_dispute_proposals(reminders, recovery_, source)
    disputes = recovery_.list_disputes()
    if not disputes:
        st.info("Aucun dossier de contentieux.")
    for dispute in disputes:
        with st.container(border=True):
            st.subheader(f"{md_escape(_client_name(reminders, dispute['client_key']))} — "
                         f"{date_fr(dispute['created_on'])}")
            st.metric("Reste dû", euros(dispute["total_due"]))
            st.caption("Dossier (récapitulatif PDF, factures, brouillon pour l'avocat) :")
            st.code(dispute["folder"], language=None, wrap_lines=True)
