"""Éléments d'interface Streamlit partagés par les pages de app/."""

import os
import re
from datetime import date
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st
from agent_factures.agent.loop import DEFAULT_COMPANY
from dotenv import load_dotenv

from agent_relances.formatting import date_fr, euros
from agent_relances.metrics import dashboard
from agent_relances.models import (
    STAGE_LABELS, Client, DraftSource, Profile, ReminderStatus, Tone, client_key,
)
from agent_relances.prepare import prepare_today, validate_reminder
from agent_relances.rules.profile import client_profile
from agent_relances.rules.schedule import UNNAMED_CLIENT
from agent_relances.sources import FacturesSourceError, InvoiceSource
from agent_relances.storage.db import connect
from agent_relances.storage.repository import ReminderStore, StoredReminder

load_dotenv()

PROFILE_LABELS = {Profile.GOOD: "Bon payeur", Profile.OCCASIONAL: "Occasionnel", Profile.CHRONIC: "Retardataire chronique"}
TONE_LABELS = {Tone.LIGHT: "Léger", Tone.NEUTRAL: "Neutre", Tone.FIRM: "Ferme"}
STATUS_LABELS = {
    ReminderStatus.TO_VALIDATE: "À valider", ReminderStatus.VALIDATED: "Validée", ReminderStatus.POSTPONED: "Reportée",
    ReminderStatus.HUMAN: "À traiter par un humain", ReminderStatus.CANCELLED: "Annulée",
}
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
        kind, text = message
        getattr(st, kind)(text)


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
    api = api_client()
    if api is None:
        st.warning("Clé d'API Claude absente : les relances seront préparées avec le modèle de texte de secours.")
    if st.button("Préparer les relances du jour", type="primary", icon=":material/mail:"):
        with st.status("Préparation des relances…", expanded=True) as box:
            try:
                result = prepare_today(
                    source, reminders, api, company(), today(),
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


def render_dashboard_page() -> None:
    source = _source_or_error()
    if source is None:
        return
    board = dashboard(source, store(), today())
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
        with st.expander(f"{md_escape(reminder.client_name)} — {md_escape(numbers)} — {when}"):
            if reminder.body:
                st.code(reminder.body, language=None, wrap_lines=True)
            if reminder.eml_path:
                st.caption(f"Déposée : {md_escape(reminder.eml_path)}")


def render_validated_page() -> None:
    _render_list(ReminderStatus.VALIDATED, "Aucune relance validée pour l'instant.")


def render_humans_page() -> None:
    _render_list(ReminderStatus.HUMAN, "Aucune facture à traiter par un humain.")


def render_history_page() -> None:
    entries = store().list_log()
    if not entries:
        st.info("Le journal est vide.")
        return
    st.dataframe(pd.DataFrame(entries), hide_index=True)
