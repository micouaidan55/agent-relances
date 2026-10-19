"""Point d'entrée Streamlit de l'agent de relance."""

import streamlit as st

st.set_page_config(page_title="Agent relances", page_icon="💶", layout="wide")

page = st.navigation(
    {
        "": [
            st.Page("app_pages/relances.py", title="Relances du jour", icon=":material/mail:", default=True),
            st.Page("app_pages/reponses.py", title="Réponses clients", icon=":material/inbox:"),
            st.Page("app_pages/echeanciers.py", title="Échéanciers", icon=":material/event_repeat:"),
            st.Page("app_pages/clients.py", title="Clients", icon=":material/group:"),
            st.Page("app_pages/tableau_de_bord.py", title="Tableau de bord", icon=":material/dashboard:"),
        ],
        "Journal": [
            st.Page("app_pages/journal_validees.py", title="Relances validées", icon=":material/check_circle:"),
            st.Page("app_pages/journal_humain.py", title="À traiter par un humain", icon=":material/person:"),
            st.Page("app_pages/journal_historique.py", title="Historique", icon=":material/history:"),
            st.Page("app_pages/contentieux.py", title="Contentieux", icon=":material/gavel:"),
        ],
    },
    position="top",
)

st.title("Agent relances")
st.caption("Relances des clients impayés, au bon moment et au bon ton. Rien n'est envoyé sans votre validation.")
st.header(page.title)
page.run()
