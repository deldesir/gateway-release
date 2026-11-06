"""Team contact sync — hand an authorized team member the team directory as a .vcf.

**A team is a RapidPro group named `Team: <Name>`.** There is no separate team
table and no separate authorization list: group membership *is* the authorization
list, and the operator manages it in RapidPro like any other group. That keeps a
single source of identity (see `app/jobs/person_sync.py`, which already mirrors
the roster into RapidPro contacts) and avoids a parallel admin store.

Three group prefixes share one `<path>`, and between them cover every case:

    Team: <path>        listed in the directory AND may request it
    Directory: <path>   listed only   (a vehicle phone, a switchboard, a visitor SIM)
    Access: <path>      may request only (a requester's private number, never published)

A path may be a tree, `Org/Region/Site`. A grant on a node covers that node, all
nodes under it, and the nodes above it — so a regional office sees its sites, a
site sees its region, and two sibling sites never see each other. Deleting any of
these groups can only remove a listing or a right; it can never add one.

Contacts that carry a `kind` field are **posts**, not people: the contact name is
the position and stays put when the person changes. Who currently holds a post is
the `holder` field — it never leaves the server, and no code path here reads it
into a card.

This module is read-only against RapidPro. Adding, changing and removing team
contacts is the operator's job, done in RapidPro — nothing here writes group
membership, so a member can never alter a directory.

Every entry point (keyword, email, QR, the notifier job) converges here:

    caller_teams(urn) -> collect_contacts(teams) -> build_vcf() -> deliver_vcf()

Delivery reality: a .vcf cannot ride the WhatsApp channel through RapidPro —
courier posts the wrong payload key to wuzapi and the whole message is dropped.
So the file goes out by email (the bridge's /send-file path) and WhatsApp gets a
short confirmation. `app/api/middleware/wuzapi_client.py` can deliver the file
directly on WhatsApp; that is wired in a later phase.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import unicodedata
import re
import tempfile
import threading
import urllib.parse
from datetime import datetime, timezone

from loguru import logger

from app.utils.vcard import Contact, build_vcard, build_vcf

# Groups are teams only when they carry this prefix. It keeps the access tiers
# (Admins/Staff/Teachers) and RapidPro's own system groups from ever being
# mistaken for a team.
TEAM_PREFIX = os.getenv("CONTACT_SYNC_TEAM_PREFIX", "Team: ")
DIRECTORY_PREFIX = os.getenv("CONTACT_SYNC_DIRECTORY_PREFIX", "Directory: ")
ACCESS_PREFIX = os.getenv("CONTACT_SYNC_ACCESS_PREFIX", "Access: ")
PATH_SEP = "/"

# Optional function catalog: {"functions": {"<code>": {"title": {"en": …, "fr": …}}}}.
# Lives under data/ (never in git) — the codes and titles belong to whoever runs
# the box. Without the file, cards simply carry no TITLE line.
CATALOG_FILE = os.getenv(
    "CONTACT_SYNC_CATALOG", "/opt/iiab/ai-gateway/data/function_catalog.json"
)
TITLE_LANG = os.getenv("CONTACT_SYNC_TITLE_LANG", "en")
# A reader whose language the catalog lacks gets the nearest one it has.
TITLE_FALLBACKS = os.getenv("CONTACT_SYNC_TITLE_FALLBACKS", "ht=fr")
DEFAULT_LANG = os.getenv("CONTACT_SYNC_LANG", "en")

# Gathering a directory and handing it to SMTP takes longer than RiveBot's
# 10s macro timeout, which would show the member "timed out" while the file
# actually went out. So the tool authorizes synchronously, answers at once, and
# delivers on a background thread. Tests set this False to assert inline.
DELIVER_IN_BACKGROUND = os.getenv("CONTACT_SYNC_BACKGROUND", "1") == "1"

# A second request for the same directory within this many minutes is answered
# without sending again: repeated full imports duplicate contacts on phones, and a
# number firing out the same document over and over looks like spam to WhatsApp.
# 0 disables it.
COOLDOWN_MINUTES = float(os.getenv("CONTACT_SYNC_COOLDOWN_MINUTES", "10"))

# Who a refused caller should ask, named in the refusal ("the ICT supervisor").
ADMIN_HINT = os.getenv("CONTACT_SYNC_ADMIN_HINT", "")
# The directory may live in its own RapidPro workspace: its API token then
# selects that workspace for every read and write below, and its own WhatsApp
# line sends the contact cards. Both empty = the assistant's workspace and line.
DIRECTORY_TOKEN = os.getenv("CONTACT_SYNC_RAPIDPRO_TOKEN", "").strip()
DIRECTORY_CHANNEL = os.getenv("CONTACT_SYNC_WUZAPI_CHANNEL", "").strip()
_RP_AUTH = {"token": DIRECTORY_TOKEN} if DIRECTORY_TOKEN else {}
# Tappable choices (WhatsApp buttons/lists via the flow's quick replies). Off
# = plain numbered text only, for phones on which interactive messages do not show.
TAPPABLE = os.getenv("CONTACT_SYNC_TAPPABLE", "1") == "1"
# List rows carry their section as "Section|Title" once the WhatsApp channel
# (courier) knows to read it; off = flat rows.
LIST_SECTIONS = os.getenv("CONTACT_SYNC_LIST_SECTIONS", "1") == "1"

# A number that sends files to people who never wrote to it is what WhatsApp's
# spam filters flag, and a linked number can be banned. So unsolicited updates
# go by WhatsApp only to members who have written to us at least once; the
# others get email, or wait until their first message. On-demand requests are
# unaffected: the requester just wrote. Set to 0 to push to everyone.
PUSH_ONLY_AFTER_CONTACT = os.getenv("CONTACT_SYNC_PUSH_ONLY_AFTER_CONTACT", "1") == "1"

# Short names people can type for a level: "bld=Org/Region/SiteB,hq=Org". The
# operator's own data, so it lives in the environment, not in this code.
LEVEL_ALIASES = os.getenv("CONTACT_SYNC_LEVEL_ALIASES", "")

# Up to this many contacts go out as native WhatsApp contact cards (tap to save,
# no file to open); more go as one .vcf file.
CARD_LIMIT = int(os.getenv("CONTACT_SYNC_CARD_LIMIT", "5"))

# Display names for levels: "Org=Head office,Org/Region/SiteB=Site B". A level
# without one shows the last part of its path. No commas inside a name.
LEVEL_NAMES = os.getenv("CONTACT_SYNC_LEVEL_NAMES", "")

# A post whose `scope` field says this is also listed, on its own, for the sibling
# levels of its level: the other sites under the same parent see it, and nothing
# else of that level. Meant for managers who need their counterparts.
MISSION_SCOPE = "mission"
MISSION_MARK = "#mission"

_PAGE_LIMIT = 20  # safety stop when following API pagination

_T = {
    "en": {
        "denied": "You are not on a team contact list. Ask {admin} to add you.",
        "admin": "the administrator",
        "no_email": "No email address is on file for you, so the contact file can't be sent. Ask the administrator to add one.",
        "unknown_team": "No team called '{team}' is available to you. Yours: {teams}.",
        "empty": "The {team} contact list is empty.",
        "sent": "Sent {n} contact(s) from {team} to {email}. Open the attached .vcf to add them.",
        "sent_here": "Here are {n} contact(s) from {team}. Open the attached file to add them.",
        "sending_email": "Sending {n} contact(s) from {team} to {email} — check your inbox in a moment.",
        "sending_here": "Sending {n} contact(s) from {team} to you here now…",
        "failed": "Couldn't send the contact file. Please try again later.",
        "delta_body": "{team} — updated contact(s): {names}.\n\nOpen the attached .vcf to add them to your phone.",
        "removed_note": "\n\nNo longer on the team: {names}. A contact file cannot delete anything, so please remove them from your phone by hand.",
        "subject": "{team} — contacts",
        "body": "Attached: {n} contact(s) from {team}.\n\nOpen the .vcf attachment to add them to your phone (iPhone: from Mail or Files › Add All Contacts; WhatsApp shows only the first). If some are already saved on your phone, importing may duplicate them on certain phones — your Contacts app's 'merge duplicates' tool cleans that up in one tap.",
        "cooldown": "The {team} contacts were sent to you {minutes} min ago. If nothing arrived, ask again in {wait} min.",
        "push_started": "Checking every directory now and sending what changed. The summary will come by email.",
        "up_to_date": "You already have the current {team} directory ({n} contacts). New or reset phone? Send *sync all* to get everything again.",
        "no_match": "Nothing in your directory matches \u201c{term}\u201d. Send *menu* to see your choices.",
        "sending_changes_here": "Sending the {n} new or changed contact(s) from {team} to you here now\u2026",
        "sending_changes_email": "Sending the {n} new or changed contact(s) from {team} to {email} \u2014 check your inbox in a moment.",
        "menu_title": "📇 *Contact directory*",
        "dir_cat_number": "A number changed",
        "dir_cat_holder": "A post is vacant or has a new holder",
        "dir_cat_wrong": "A name or a post is wrong",
        "dir_cat_other": "Something else",
        "dir_describe_body": "{category}\nWhich post, and what is the correct information?",
        "welcome": "Welcome to the mission's ICT line. This number sends the contact directory and takes ICT requests.\nSend *sync* for your contacts or *menu* for all options.",
        "ict_cat_hardware": "Computer, printer, equipment",
        "ict_cat_network": "Internet, Wi-Fi, network",
        "ict_cat_accounts": "Accounts, e-mail, access",
        "ict_cat_radio": "Radio, phone, satellite",
        "ict_cat_other": "Something else",
        "ict_describe_more": "Please give a few more words, so the request can be handled. 0 to cancel.",
        "ict_help_solved": "Glad it helped. Send *menu* whenever you need something else.",
        "ict_opened": "Request #{number} recorded. {admin} will follow up here.",
        "ict_opened_no_number": "Request recorded. {admin} will follow up here.",
        "ict_added_to_open": "Added to your open request #{number}. {admin} will follow up here.",
        "ict_none": "You have no ICT request on record. Send *menu* to make one.",
        "ict_mine": "Your ICT requests:\n{tickets}",
        "ict_status_open": "open",
        "ict_status_closed": "closed",
        "ict_closed_subject": "ICT request #{number} closed",
        "ict_closed": "Your ICT request #{number} is closed. Note: {note}\n\nIf the problem comes back, send *menu* to open a new request.",
        "menu_invalid": "Choice not recognised. Reply with one of the numbers below.",
        "menu_bye": "Goodbye. Send *menu* any time.",
        "lang_set": "Language: English.",
        "dept_medical": "Medical",
        "dept_log": "Logistics & supply",
        "dept_admin": "Admin & HR",
        "dept_ops": "Operations",
        "dept_assets": "Vehicles & phones",
        "mission_suffix": "managers"
    },
    "ht": {
        "denied": "Ou pa nan yon lis kontak ekip. Mande {admin} pou l ajoute w.",
        "admin": "administratè a",
        "no_email": "Nou pa gen adrès imèl pou ou, kidonk nou pa ka voye fichye kontak la. Mande administratè a pou l ajoute youn.",
        "unknown_team": "Pa gen ekip ki rele '{team}' ki disponib pou ou. Pa w yo: {teams}.",
        "empty": "Lis kontak {team} la vid.",
        "sent": "Nou voye {n} kontak nan {team} bay {email}. Louvri fichye .vcf la pou ajoute yo.",
        "sent_here": "Men {n} kontak nan {team}. Louvri fichye a pou ajoute yo.",
        "sending_email": "N ap voye {n} kontak nan {team} bay {email} — tcheke bwat ou nan yon moman.",
        "sending_here": "N ap voye {n} kontak nan {team} ba ou la a kounye a…",
        "failed": "Nou pa t ka voye fichye kontak la. Tanpri eseye ankò pita.",
        "delta_body": "{team} — kontak ki chanje: {names}.\n\nLouvri fichye .vcf la pou ajoute yo nan telefòn ou.",
        "removed_note": "\n\nMoun sa yo pa nan ekip la ankò: {names}. Yon fichye kontak pa ka efase anyen — tanpri retire yo nan telefòn ou ou-menm.",
        "subject": "{team} — kontak",
        "body": "Anpil: {n} kontak nan {team}.\n\nLouvri atachman .vcf la pou ajoute yo nan telefòn ou (iPhone : nan Mail oswa Fichye › Ajoute tout kontak yo ; WhatsApp montre premye a sèlman). Si kèk ladan yo deja nan telefòn ou, enpòte a ka double yo sou sèten telefòn — zouti 'merge duplicates' nan app Kontak ou a netwaye sa fasil.",
        "cooldown": "Nou te voye kontak {team} yo ba ou sa gen {minutes} minit. Si w pa resevwa anyen, mande ankò nan {wait} minit.",
        "push_started": "N ap tcheke tout lis yo kounye a epi voye sa ki chanje. Rezime a ap vini pa imèl.",
        "up_to_date": "Ou gen d\u00e8nye lis {team} la deja ({n} kontak). Nouvo telef\u00f2n oswa telef\u00f2n ki reyinisyalize? Voye *sync all* pou resevwa tout ank\u00f2.",
        "no_match": "Anyen nan any\u00e8 w la pa koresponn ak \u201c{term}\u201d. Voye *menu* pou w\u00e8 chwa yo.",
        "sending_changes_here": "N ap voye {n} kontak ki nouvo oswa ki chanje nan {team} ba ou la a\u2026",
        "sending_changes_email": "N ap voye {n} kontak ki nouvo oswa ki chanje nan {team} bay {email} \u2014 tcheke bwat ou nan yon moman.",
        "menu_title": "📇 *Anyè kontak*",
        "dir_cat_number": "Yon nimewo chanje",
        "dir_cat_holder": "Yon p\u00f2s vid oswa gen yon nouvo moun",
        "dir_cat_wrong": "Yon non oswa yon p\u00f2s pa k\u00f2r\u00e8k",
        "dir_cat_other": "L\u00f2t bagay",
        "dir_describe_body": "{category}\nKi p\u00f2s, epi ki bon enf\u00f2masyon an?",
        "welcome": "Byenveni sou liy ICT misyon an. Nimewo sa a voye anyè kontak la epi resevwa demann ICT.\nVoye *sync* pou kontak ou yo oswa *menu* pou tout opsyon yo.",
        "ict_cat_hardware": "\u00d2dinat\u00e8, enprimant, materyèl",
        "ict_cat_network": "Ent\u00e8n\u00e8t, Wi-Fi, rezo",
        "ict_cat_accounts": "Kont, im\u00e8l, aks\u00e8",
        "ict_cat_radio": "Radyo, telef\u00f2n, satelit",
        "ict_cat_other": "L\u00f2t bagay",
        "ict_describe_more": "Tanpri bay k\u00e8k mo anplis, pou demann lan ka trete. 0 pou anile.",
        "ict_help_solved": "Kontan sa te ede w. Voye *menu* l\u00e8 w bezwen yon l\u00f2t bagay.",
        "ict_opened": "Demann #{number} anrejistre. {admin} ap reponn ou la a.",
        "ict_opened_no_number": "Demann anrejistre. {admin} ap reponn ou la a.",
        "ict_added_to_open": "Nou ajoute l nan demann ou an ki louvri toujou (#{number}). {admin} ap reponn ou la a.",
        "ict_none": "Ou pa gen okenn demann ICT. Voye *menu* pou f\u00e8 youn.",
        "ict_mine": "Demann ICT ou yo:\n{tickets}",
        "ict_status_open": "louvri",
        "ict_status_closed": "f\u00e8men",
        "ict_closed_subject": "Demann ICT #{number} f\u00e8men",
        "ict_closed": "Demann ICT ou an #{number} f\u00e8men. N\u00f2t: {note}\n\nSi pwobl\u00e8m nan retounen, voye *menu* pou louvri yon nouvo demann.",
        "menu_invalid": "Nou pa rekonèt chwa sa a. Reponn ak youn nan nimewo ki anba yo.",
        "menu_bye": "Orevwa. Voye *menu* nenpòt lè.",
        "lang_set": "Lang: krey\u00f2l.",
        "dept_medical": "Medikal",
        "dept_log": "Lojistik ak apwovizyonman",
        "dept_admin": "Administrasyon ak RH",
        "dept_ops": "Operasyon",
        "dept_assets": "Machin ak telef\u00f2n",
        "mission_suffix": "responsab yo"
    },
    "fr": {
        "denied": "Tu ne figures sur aucune liste de contacts. Demande à {admin} de t’ajouter.",
        "admin": "l\u2019administrateur",
        "no_email": "Aucune adresse e-mail n’est enregistrée pour toi : le fichier de contacts ne peut pas être envoyé. Demande à l’administrateur d’en ajouter une.",
        "unknown_team": "Aucune liste « {team} » ne t’est accessible. Les tiennes : {teams}.",
        "empty": "La liste de contacts {team} est vide.",
        "sent": "{n} contact(s) de {team} envoyé(s) à {email}. Ouvre le fichier .vcf joint pour les ajouter.",
        "sent_here": "Voici {n} contact(s) de {team}. Ouvre le fichier joint pour les ajouter.",
        "sending_email": "📧 Envoi de {n} contact(s) — {team} — à {email}. Vérifie ta boîte de réception dans un instant.",
        "sending_here": "📤 Envoi de {n} contact(s) — {team}. Ils arrivent dans un instant.\nAndroid : touche le fichier › ouvrir avec Contacts › ton compte Google › Importer.\niPhone : WhatsApp n’affiche que le premier contact. Touche le fichier › Partager › Enregistrer dans Fichiers, puis ouvre-le dans Fichiers › Ajouter tous les contacts.",
        "failed": "Impossible d’envoyer le fichier de contacts. Réessaie plus tard.",
        "delta_body": "{team} : contact(s) mis à jour : {names}.\n\nOuvre le fichier .vcf joint pour les ajouter à ton téléphone.",
        "removed_note": "\n\nNe figurent plus dans la liste : {names}. Un fichier de contacts ne peut rien supprimer : retire-les toi-même de ton téléphone.",
        "subject": "{team}\u00a0: contacts",
        "body": "Ci-joint : {n} contact(s) de {team}.\n\nOuvre le fichier .vcf pour les ajouter à ton téléphone (iPhone : depuis Mail ou Fichiers › Ajouter tous les contacts ; WhatsApp n’en affiche que le premier). Si certains y sont déjà, l’importation peut créer des doublons sur certains téléphones : l’outil « Fusionner les doublons » de ton application Contacts les regroupe en un geste.",
        "cooldown": "Les contacts {team} t’ont été envoyés il y a {minutes} min. Si rien n’est arrivé, redemande dans {wait} min.",
        "push_started": "V\u00e9rification de toutes les listes et envoi des changements en cours. Le r\u00e9sum\u00e9 arrivera par e-mail.",
        "up_to_date": "Tu as déjà la liste {team} à jour ({n} contacts). Nouveau téléphone ou téléphone réinitialisé ? Envoie *sync all* pour tout recevoir à nouveau.",
        "no_match": "Rien dans ton annuaire ne correspond à « {term} ». Envoie *menu* pour voir les choix.",
        "sending_changes_here": "📤 Envoi de {n} contact(s) nouveaux ou modifiés — {team}. Ils arrivent dans un instant.\nAndroid : touche le fichier › ouvrir avec Contacts › ton compte Google › Importer.\niPhone : WhatsApp n’affiche que le premier contact. Touche le fichier › Partager › Enregistrer dans Fichiers, puis ouvre-le dans Fichiers › Ajouter tous les contacts.",
        "sending_changes_email": "📧 Envoi de {n} contact(s) nouveaux ou modifiés — {team} — à {email}. Vérifie ta boîte de réception dans un instant.",
        "menu_title": "📇 *Annuaire des contacts*",
        "dir_cat_number": "Un num\u00e9ro a chang\u00e9",
        "dir_cat_holder": "Un poste est vacant ou a un nouveau titulaire",
        "dir_cat_wrong": "Un nom ou un poste est erron\u00e9",
        "dir_cat_other": "Autre chose",
        "dir_describe_body": "{category}\nQuel poste, et quelle est la bonne information\u00a0?",
        "welcome": "Bienvenue sur la ligne ICT de la mission. Ce numéro envoie l’annuaire des contacts et reçoit les demandes ICT.\nEnvoie *sync* pour tes contacts ou *menu* pour toutes les options.",
        "ict_cat_hardware": "Ordinateur, imprimante, mat\u00e9riel",
        "ict_cat_network": "Internet, Wi-Fi, r\u00e9seau",
        "ict_cat_accounts": "Comptes, e-mail, acc\u00e8s",
        "ict_cat_radio": "Radio, t\u00e9l\u00e9phone, satellite",
        "ict_cat_other": "Autre chose",
        "ict_describe_more": "Quelques mots de plus, s’il te plaît, pour que la demande puisse être traitée. 0 pour annuler.",
        "ict_help_solved": "Content que ça ait aidé. Envoie *menu* dès que tu as besoin d’autre chose.",
        "ict_opened": "Demande n° {number} enregistrée. {admin} te répondra ici.",
        "ict_opened_no_number": "Demande enregistrée. {admin} te répondra ici.",
        "ict_added_to_open": "Ajouté à ta demande en cours n° {number}. {admin} te répondra ici.",
        "ict_none": "Aucune demande ICT à ton nom. Envoie *menu* pour en faire une.",
        "ict_mine": "Tes demandes ICT :\n{tickets}",
        "ict_status_open": "en cours",
        "ict_status_closed": "clos\u00e9e",
        "ict_closed_subject": "Demande ICT n\u00b0\u00a0{number} clos\u00e9e",
        "ict_closed": "Ta demande ICT n° {number} est closée. Note : {note}\n\nSi le problème revient, envoie *menu* pour ouvrir une nouvelle demande.",
        "menu_invalid": "Choix non reconnu. Répondez par l’un des numéros ci-dessous.",
        "menu_bye": "À bientôt. Envoie *menu* à tout moment.",
        "lang_set": "Langue\u00a0: fran\u00e7ais.",
        "dept_medical": "M\u00e9dical",
        "dept_log": "Logistique et approvisionnement",
        "dept_admin": "Administration et RH",
        "dept_ops": "Op\u00e9rations",
        "dept_assets": "V\u00e9hicules et t\u00e9l\u00e9phones",
        "mission_suffix": "responsables"
    }
}


# RapidPro stores contact language as ISO-639-3 (`language varchar(3)`), so
# "hat"/"eng" are what actually arrive — not the 2-letter codes.
# The menu, as option lists: section and option titles, descriptions, prompts.
_MENU_STRINGS = {
    "en": {
        "sec_contacts": "Contacts",
        "sec_ict": "ICT support",
        "sec_settings": "Settings",
        "sec_sites": "Sites",
        "sec_depts": "Departments",
        "sec_categories": "Categories",
        "opt_updates": "Updates for me",
        "opt_updates_d": "New or changed since your last sync",
        "opt_all": "Full directory",
        "opt_all_d": "Everything, for a new phone",
        "opt_sites": "By site",
        "opt_sites_d": "One site's contacts",
        "opt_depts": "By department",
        "opt_depts_d": "Medical, logistics, admin, operations…",
        "opt_find": "Find a post",
        "opt_find_d": "By name or code: PMR, LOGCO…",
        "opt_ict_new": "New ICT request",
        "opt_ict_new_d": "Equipment, network, accounts, radio",
        "opt_ict_mine": "My requests",
        "opt_ict_mine_d": "Where your requests stand",
        "opt_dir_fix": "Directory correction",
        "opt_dir_fix_d": "A number, holder or post changed",
        "opt_lang": "Language",
        "opt_lang_d": "Français · Kreyòl · English",
        "ict_title": "🛠 *ICT support*",
        "sites_title": "*By site*",
        "depts_title": "*By department*",
        "find_title": "*Find a post*",
        "lang_title": "*Language*",
        "ict_new_title": "*New ICT request*",
        "dir_fix_title": "*Directory correction*",
        "ask_main": "What would you like to do?",
        "ask_site": "Which site?",
        "ask_dept": "Which department?",
        "ask_lang": "Choose your language.",
        "ask_category": "What is it about?",
        "ask_changed": "What changed?",
        "find_body": "Type the post's name or code (e.g. PMR, LOGCO).",
        "back": "Back",
        "exit": "Exit",
        "cancel": "Cancel",
        "prompt_number": "Reply with a number.",
        "lang_fr": "Français",
        "lang_ht": "Kreyòl",
        "lang_en": "English",
        "dept_medical_d": "Medical and paramedical team",
        "dept_log_d": "Logistics, supply, technical",
        "dept_admin_d": "Finance, administration, HR",
        "dept_ops_d": "Coordination and project management",
        "dept_assets_d": "Vehicles and shared lines",
        "ict_cat_hardware_t": "Equipment",
        "ict_cat_network_t": "Network & Internet",
        "ict_cat_accounts_t": "Accounts & access",
        "ict_cat_radio_t": "Radio & phones",
        "ict_cat_other_t": "Other",
        "dir_cat_number_t": "Number",
        "dir_cat_holder_t": "Holder",
        "dir_cat_wrong_t": "Name or post",
        "dir_cat_other_t": "Other",
        "help_body": "{category}\nBefore opening a request, try this:\n{tip}",
        "help_solved": "Solved, thanks",
        "help_open": "Open the request",
        "describe_body": "{category}\nDescribe the problem in one or two sentences: where, since when, what you see.",
        "menu_invalid": "Choice not recognised.",
        "whole_directory": "the whole directory",
        "menu_button": "Menu",
        "sending_here": "📤 Sending {n} contact(s) — {team}. They will arrive in a moment.\nAndroid: tap the file › open with Contacts › your Google account › Import.\niPhone: WhatsApp shows only the first contact. Tap the file › Share › Save to Files, then open it in Files › Add All Contacts.",
        "sending_changes_here": "📤 Sending {n} new or changed contact(s) — {team}. They will arrive in a moment.\nAndroid: tap the file › open with Contacts › your Google account › Import.\niPhone: WhatsApp shows only the first contact. Tap the file › Share › Save to Files, then open it in Files › Add All Contacts.",
        "sending_email": "📧 Sending {n} contact(s) — {team} — to {email}. Check your inbox in a moment.",
        "sending_changes_email": "📧 Sending {n} new or changed contact(s) — {team} — to {email}. Check your inbox in a moment."
    },
    "ht": {
        "sec_contacts": "Kontak",
        "sec_ict": "Sipò ICT",
        "sec_settings": "Paramèt",
        "sec_sites": "Sit yo",
        "sec_depts": "Depatman yo",
        "sec_categories": "Kategori yo",
        "opt_updates": "Mizajou pou mwen",
        "opt_updates_d": "Sa ki nouvo depi dènye sync ou",
        "opt_all": "Tout anyè a",
        "opt_all_d": "Tout kontak yo, pou yon nouvo telefòn",
        "opt_sites": "Pa sit",
        "opt_sites_d": "Kontak yon sit",
        "opt_depts": "Pa depatman",
        "opt_depts_d": "Medikal, lojistik, admin, operasyon…",
        "opt_find": "Chèche yon pòs",
        "opt_find_d": "Pa non oswa kòd: PMR, LOGCO…",
        "opt_ict_new": "Nouvo demann ICT",
        "opt_ict_new_d": "Materyèl, rezo, kont, radyo",
        "opt_ict_mine": "Demann mwen yo",
        "opt_ict_mine_d": "Kote demann ou yo rive",
        "opt_dir_fix": "Koreksyon anyè",
        "opt_dir_fix_d": "Nimewo, moun oswa pòs ki chanje",
        "opt_lang": "Lang",
        "opt_lang_d": "Français · Kreyòl · English",
        "ict_title": "🛠 *Sipò ICT*",
        "sites_title": "*Pa sit*",
        "depts_title": "*Pa depatman*",
        "find_title": "*Chèche yon pòs*",
        "lang_title": "*Lang*",
        "ict_new_title": "*Nouvo demann ICT*",
        "dir_fix_title": "*Koreksyon anyè*",
        "ask_main": "Kisa ou vle fè?",
        "ask_site": "Ki sit?",
        "ask_dept": "Ki depatman?",
        "ask_lang": "Chwazi lang ou.",
        "ask_category": "Sou kisa li ye?",
        "ask_changed": "Kisa ki chanje?",
        "find_body": "Ekri non oswa kòd pòs la (egz. PMR, LOGCO).",
        "back": "Retounen",
        "exit": "Kite",
        "cancel": "Anile",
        "prompt_number": "Reponn ak yon nimewo.",
        "lang_fr": "Français",
        "lang_ht": "Kreyòl",
        "lang_en": "English",
        "dept_log": "Lojistik ak sipò",
        "dept_medical_d": "Ekip medikal ak paramedikal",
        "dept_log_d": "Lojistik, apwovizyonman, teknik",
        "dept_admin_d": "Finans, administrasyon, RH",
        "dept_ops_d": "Kowòdinasyon ak jesyon pwojè",
        "dept_assets_d": "Machin ak liy pataje",
        "ict_cat_hardware_t": "Materyèl",
        "ict_cat_network_t": "Rezo & Entènèt",
        "ict_cat_accounts_t": "Kont & aksè",
        "ict_cat_radio_t": "Radyo & telefòn",
        "ict_cat_other_t": "Lòt",
        "dir_cat_number_t": "Nimewo",
        "dir_cat_holder_t": "Moun ki nan pòs la",
        "dir_cat_wrong_t": "Non oswa pòs",
        "dir_cat_other_t": "Lòt",
        "help_body": "{category}\nAnvan ou louvri yon demann, eseye sa a:\n{tip}",
        "help_solved": "Rezoud, mèsi",
        "help_open": "Louvri demann lan",
        "describe_body": "{category}\nDekri pwoblèm nan nan youn oswa de fraz: ki kote, depi kilè, sa w wè.",
        "menu_invalid": "Nou pa rekonèt chwa sa a.",
        "whole_directory": "tout anyè a",
        "menu_button": "Menu",
        "sending_here": "📤 N ap voye {n} kontak — {team}. Y ap rive nan yon moman.\nAndroid : touche fichye a › louvri ak Kontak › kont Google ou › Enpòte.\niPhone : WhatsApp montre premye kontak la sèlman. Touche fichye a › Pataje › Sove nan Fichye, epi louvri l nan Fichye › Ajoute tout kontak yo.",
        "sending_changes_here": "📤 N ap voye {n} kontak ki nouvo oswa ki chanje — {team}. Y ap rive nan yon moman.\nAndroid : touche fichye a › louvri ak Kontak › kont Google ou › Enpòte.\niPhone : WhatsApp montre premye kontak la sèlman. Touche fichye a › Pataje › Sove nan Fichye, epi louvri l nan Fichye › Ajoute tout kontak yo.",
        "sending_email": "📧 N ap voye {n} kontak — {team} — bay {email}. Tcheke bwat ou nan yon moman.",
        "sending_changes_email": "📧 N ap voye {n} kontak ki nouvo oswa ki chanje — {team} — bay {email}. Tcheke bwat ou nan yon moman."
    },
    "fr": {
        "sec_contacts": "Contacts",
        "sec_ict": "Support ICT",
        "sec_settings": "Réglages",
        "sec_sites": "Sites",
        "sec_depts": "Départements",
        "sec_categories": "Catégories",
        "opt_updates": "Mises à jour",
        "opt_updates_d": "Nouveautés depuis ta dernière synchro",
        "opt_all": "Annuaire complet",
        "opt_all_d": "Tous les contacts, pour un nouveau téléphone",
        "opt_sites": "Par site",
        "opt_sites_d": "Les contacts d’un site",
        "opt_depts": "Par département",
        "opt_depts_d": "Médical, logistique, admin, opérations…",
        "opt_find": "Rechercher un poste",
        "opt_find_d": "Par nom ou sigle : PMR, LOGCO…",
        "opt_ict_new": "Nouvelle demande ICT",
        "opt_ict_new_d": "Matériel, réseau, comptes, radio",
        "opt_ict_mine": "Mes demandes",
        "opt_ict_mine_d": "Où en sont tes demandes",
        "opt_dir_fix": "Correction d’annuaire",
        "opt_dir_fix_d": "Numéro, titulaire ou poste modifié",
        "opt_lang": "Langue",
        "opt_lang_d": "Français · Kreyòl · English",
        "ict_title": "🛠 *Support ICT*",
        "sites_title": "*Par site*",
        "depts_title": "*Par département*",
        "find_title": "*Rechercher un poste*",
        "lang_title": "*Langue*",
        "ict_new_title": "*Nouvelle demande ICT*",
        "dir_fix_title": "*Correction d’annuaire*",
        "ask_main": "Que veux-tu faire ?",
        "ask_site": "Quel site ?",
        "ask_dept": "Quel département ?",
        "ask_lang": "Choisis ta langue.",
        "ask_category": "De quoi s’agit-il ?",
        "ask_changed": "Qu’est-ce qui a changé ?",
        "find_body": "Tape le nom ou le sigle du poste (ex. PMR, LOGCO).",
        "back": "Retour",
        "exit": "Quitter",
        "cancel": "Annuler",
        "prompt_number": "Réponds par un numéro.",
        "lang_fr": "Français",
        "lang_ht": "Kreyòl",
        "lang_en": "English",
        "dept_log": "Logistique & supply",
        "dept_medical_d": "Équipe médicale et paramédicale",
        "dept_log_d": "Logistique, approvisionnement, technique",
        "dept_admin_d": "Finance, administration, RH",
        "dept_ops_d": "Coordination et gestion de projet",
        "dept_assets_d": "Véhicules et lignes partagées",
        "ict_cat_hardware_t": "Matériel",
        "ict_cat_network_t": "Réseau & Internet",
        "ict_cat_accounts_t": "Comptes & accès",
        "ict_cat_radio_t": "Radio & téléphonie",
        "ict_cat_other_t": "Autre",
        "dir_cat_number_t": "Numéro",
        "dir_cat_holder_t": "Titulaire",
        "dir_cat_wrong_t": "Nom ou poste",
        "dir_cat_other_t": "Autre",
        "help_body": "{category}\nAvant d’ouvrir une demande, essaie ceci :\n{tip}",
        "help_solved": "Résolu, merci",
        "help_open": "Ouvrir la demande",
        "describe_body": "{category}\nDécris le problème en une ou deux phrases : où, depuis quand, ce que tu vois.",
        "menu_invalid": "Choix non reconnu.",
        "whole_directory": "tout l’annuaire",
        "menu_button": "Menu",
        "sending_here": "📤 Envoi de {n} contact(s) — {team}. Ils arrivent dans un instant.\nAndroid : touche le fichier › ouvrir avec Contacts › ton compte Google › Importer.\niPhone : WhatsApp n’affiche que le premier contact. Touche le fichier › Partager › Enregistrer dans Fichiers, puis ouvre-le dans Fichiers › Ajouter tous les contacts.",
        "sending_changes_here": "📤 Envoi de {n} contact(s) nouveaux ou modifiés — {team}. Ils arrivent dans un instant.\nAndroid : touche le fichier › ouvrir avec Contacts › ton compte Google › Importer.\niPhone : WhatsApp n’affiche que le premier contact. Touche le fichier › Partager › Enregistrer dans Fichiers, puis ouvre-le dans Fichiers › Ajouter tous les contacts.",
        "sending_email": "📧 Envoi de {n} contact(s) — {team} — à {email}. Vérifie ta boîte de réception dans un instant.",
        "sending_changes_email": "📧 Envoi de {n} contact(s) nouveaux ou modifiés — {team} — à {email}. Vérifie ta boîte de réception dans un instant."
    }
}
for _code, _table in _MENU_STRINGS.items():
    _T[_code].update(_table)

_LANG_ALIASES = {"ht": "ht", "hat": "ht", "en": "en", "eng": "en", "fr": "fr", "fra": "fr", "fre": "fr"}


def _t(key: str, lang: str, **fmt) -> str:
    code = (lang or DEFAULT_LANG).strip().lower()
    table = _T[_LANG_ALIASES.get(code, "en")]
    if "{admin}" in table.get(key, _T["en"].get(key, "")):
        fmt.setdefault("admin", ADMIN_HINT or table.get("admin", _T["en"]["admin"]))
    return table.get(key, _T["en"][key]).format(**fmt)


def _mask(urn: str) -> str:
    """Identify a caller in logs without writing a full number or address."""
    value = urn.split(":", 1)[-1]
    if "@" in value:
        return value[0] + "***@" + value.split("@", 1)[1]
    return "***" + value[-4:] if len(value) > 4 else "***"


def _rev() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _slug(text: str) -> str:
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", str(text or ""))
                    if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-") or "team"


# ── RapidPro reads ───────────────────────────────────────────────────────────


def _rp_paged(endpoint: str, params: dict) -> list[dict]:
    """Collect every page of an API v2 list endpoint. Returns [] on any error."""
    from app.graph.tools.rapidpro import _rp_api

    results: list[dict] = []
    data = _rp_api("GET", endpoint, params=params, **_RP_AUTH)
    pages = 0
    while True:
        if "error" in data:
            logger.error(f"[contact_sync] RapidPro read failed on {endpoint}: {data['error']}")
            return []
        results.extend(data.get("results") or [])
        nxt = data.get("next")
        pages += 1
        if not nxt or pages >= _PAGE_LIMIT:
            if nxt:
                logger.warning(f"[contact_sync] stopped paginating {endpoint} at {_PAGE_LIMIT} pages")
            return results
        # `next` is an absolute URL; hand it back through the same helper by
        # re-using its cursor param rather than re-implementing auth.
        cursor = _cursor_of(nxt)
        if not cursor:
            return results
        data = _rp_api("GET", endpoint, params={**params, "cursor": cursor}, **_RP_AUTH)


def _cursor_of(url: str) -> str:
    """Pull the cursor out of a `next` URL, decoded.

    DRF encodes cursors with standard base64, so they can contain `+`, `/` and
    `=`, which arrive percent-escaped in the URL. Without unquoting here, passing
    the raw text back through `params=` re-encodes the `%` and the cursor is
    rejected — the second page never loads and the directory is silently short.
    """
    match = re.search(r"[?&]cursor=([^&]+)", url or "")
    return urllib.parse.unquote(match.group(1)) if match else ""


def _split_group(name: str) -> tuple[str, str] | None:
    """('team' | 'directory' | 'access', path) for one of our groups, else None."""
    for role, prefix in (("team", TEAM_PREFIX), ("directory", DIRECTORY_PREFIX),
                         ("access", ACCESS_PREFIX)):
        if name.startswith(prefix):
            path = PATH_SEP.join(part.strip() for part in name[len(prefix):].split(PATH_SEP))
            return (role, path) if path.strip(PATH_SEP + " ") else None
    return None


def _our_groups() -> dict[str, list[tuple[str, str]]]:
    """path -> [(role, exact group name)] for every group of ours. {} on error."""
    index: dict[str, list[tuple[str, str]]] = {}
    for group in _rp_paged("groups.json", {}):
        name = group.get("name") or ""
        parsed = _split_group(name)
        if parsed:
            index.setdefault(parsed[1], []).append((parsed[0], name))
    return index


def list_teams() -> list[str]:
    """Every directory node, as its team key, e.g. ['Team: QA']. Sorted, fail-closed.

    A node exists as soon as something lists contacts under its path — a `Team:`
    group, a `Directory:` group, or both. The key keeps the `Team: ` spelling
    whichever of them it was, so the notifier's state file stays valid across
    the move from flat teams to a tree.
    """
    return sorted(
        TEAM_PREFIX + path
        for path, groups in _our_groups().items()
        if any(role in ("team", "directory") for role, _ in groups)
    )


def team_label(group_name: str) -> str:
    return group_name[len(TEAM_PREFIX):].strip() if group_name.startswith(TEAM_PREFIX) else group_name


def is_mission_node(key: str) -> bool:
    """A node key for the mission-wide posts of a level (see MISSION_SCOPE)."""
    return key.endswith(MISSION_MARK)


def node_path(key: str) -> str:
    """The level path behind a node key, mission-wide or not."""
    label = team_label(key)
    return label[: -len(MISSION_MARK)] if label.endswith(MISSION_MARK) else label


def _parent(path: str) -> str:
    return path.rsplit(PATH_SEP, 1)[0] if PATH_SEP in path else ""


def list_nodes() -> list[str]:
    """What the notifier versions: every level, plus the mission-wide subset of
    every level that has a parent (empty subsets are simply skipped)."""
    levels = list_teams()
    return levels + [key + MISSION_MARK for key in levels if _parent(team_label(key))]


def sees(grant: str, node: str) -> bool:
    """Whether a grant on `grant` covers `node`: itself, below it, or above it.

    Siblings never match — "Org/A" and "Org/B" share a parent, not a directory.
    """
    grant, node = grant.casefold(), node.casefold()
    return (node == grant or node.startswith(grant + PATH_SEP)
            or grant.startswith(node + PATH_SEP))


def grant_paths(contact: dict | None) -> list[str]:
    """Paths this contact may request, from `Team:`/`Access:` membership. Fail-closed."""
    if not contact or not _is_reachable(contact):
        return []
    paths = []
    for group in contact.get("groups") or []:
        parsed = _split_group(group.get("name") or "")
        if parsed and parsed[0] in ("team", "access") and parsed[1] not in paths:
            paths.append(parsed[1])
    return sorted(paths)


def listed_paths(contact: dict | None) -> list[str]:
    """Paths this contact appears under, in any role. Enough to ask ICT for help,
    not to receive a directory. Fail-closed."""
    if not contact or not _is_reachable(contact):
        return []
    return sorted({parsed[1] for group in contact.get("groups") or []
                   if (parsed := _split_group(group.get("name") or ""))})


def visible_nodes(grants: list[str]) -> list[str]:
    """Node keys the given grants cover.

    Every level a grant sees (itself, below, above), plus, for a level it does
    not see but whose parent it sees (a sibling site), that level's mission-wide
    subset only.
    """
    levels = list_teams()
    mine = [key for key in levels if any(sees(g, team_label(key)) for g in grants)]
    siblings = [key + MISSION_MARK for key in levels
                if key not in mine and _parent(team_label(key))
                and any(sees(g, _parent(team_label(key))) for g in grants)]
    # Only siblings that actually have a mission-wide post: an empty one would
    # still tell the caller that the sibling exists.
    return mine + [key for key in siblings if node_listing(key)]


def _urns_by_scheme(contact: dict, scheme: str) -> list[str]:
    out = []
    for urn in contact.get("urns") or []:
        head, _, tail = urn.partition(":")
        if head == scheme and tail:
            out.append(tail)
    return out


def _phone_of(contact: dict) -> tuple[str, ...]:
    """Prefer tel: (already E.164) then whatsapp:, deduped, formatted for a phone."""
    seen: list[str] = []
    for raw in _urns_by_scheme(contact, "tel") + _urns_by_scheme(contact, "whatsapp"):
        digits = re.sub(r"\D", "", raw)
        if not digits:
            continue
        formatted = "+" + digits
        if formatted not in seen:
            seen.append(formatted)
    return tuple(seen)


def _email_of(contact: dict) -> str:
    emails = _urns_by_scheme(contact, "mailto")
    return emails[0] if emails else ""


# API v2 serializes contact status as a word ("active"/"blocked"/"stopped"/
# "archived"); the `contacts_contact.status` column itself is a single letter
# ("A"/"B"/"S"/"V"). Accept both so this works against either shape — comparing
# only against the column value silently denies every caller.
_ACTIVE_STATUS = {"active", "a", ""}


def _is_reachable(contact: dict) -> bool:
    """Skip contacts RapidPro considers inactive — §7's 'inactive loses privileges'."""
    status = str(contact.get("status") or "").strip().lower()
    return (
        not contact.get("blocked")
        and not contact.get("stopped")
        and status in _ACTIVE_STATUS
    )


_catalog_cache: dict = {"mtime": None, "functions": {}}


def _catalog() -> dict:
    """The function catalog, re-read when the file changes. {} when there is none."""
    try:
        mtime = os.path.getmtime(CATALOG_FILE)
    except OSError:
        return {}
    if _catalog_cache["mtime"] != mtime:
        try:
            with open(CATALOG_FILE, encoding="utf-8") as fh:
                _catalog_cache["functions"] = json.load(fh).get("functions") or {}
        except (OSError, ValueError) as exc:
            logger.warning(f"[contact_sync] catalog unreadable: {exc}")
            _catalog_cache["functions"] = {}
        _catalog_cache["mtime"] = mtime
    return _catalog_cache["functions"]


def _function_code(value: str) -> str:
    """The code out of a `function` field, stored bare or as "Some Title · CODE"."""
    return (value or "").rsplit("·", 1)[-1].strip()


def title_lang(contact_language: str = "") -> str:
    """Catalog language for a reader, from their RapidPro language ('fra' → 'fr')."""
    code = (contact_language or "").strip().lower()
    code = {"eng": "en", "fra": "fr", "fre": "fr", "spa": "es", "hat": "ht", "por": "pt"}.get(code, code[:2])
    fallbacks = dict(pair.split("=", 1) for pair in TITLE_FALLBACKS.split(",") if "=" in pair)
    return fallbacks.get(code, code) or TITLE_LANG


def function_title(code: str, lang: str = "") -> str:
    titles = (_catalog().get(code) or {}).get("title") or {}
    return titles.get(lang) or titles.get(TITLE_LANG) or ""


def localize(contacts: list[Contact], contact_language: str) -> list[Contact]:
    """The same cards with job titles in the reader's language.

    Directories are fingerprinted in the default language — otherwise one edit
    would look like a different change to every reader — and only re-titled here,
    at the moment a file is built for someone.
    """
    lang = title_lang(contact_language)
    if lang == TITLE_LANG:
        return contacts
    out = []
    for contact in contacts:
        title = function_title(contact.function, lang) if contact.function else ""
        out.append(dataclasses.replace(contact, title=title) if title else contact)
    return out


def _field(raw: dict, key: str) -> str:
    return str((raw.get("fields") or {}).get(key) or "").strip()


def _to_contact(raw: dict, org: str) -> Contact:
    email = _email_of(raw)
    parts = [part for part in org.split(PATH_SEP) if part]
    is_post = bool(_field(raw, "kind"))
    code = _function_code(_field(raw, "function")) if is_post else ""
    # `holder` is deliberately absent: who sits in a post stays on the server.
    note = " · ".join(x for x in (_field(raw, "alias"), _field(raw, "base")) if x) if is_post else ""
    return Contact(
        uid=raw.get("uuid") or "",
        name=(raw.get("name") or "").strip(),
        phones=_phone_of(raw),
        emails=(email,) if email else (),
        org=parts[0] if parts else org,
        org_units=tuple(parts[1:]),
        title=function_title(code) if code else "",
        note=note,
        single_name=is_post,
        function=code,
        kind=_field(raw, "kind"),
        alias=_field(raw, "alias"),
    )


def team_members(group_name: str) -> list[dict]:
    """Raw RapidPro contacts in one exact group, active ones only."""
    members = _rp_paged("contacts.json", {"group": group_name})
    return [c for c in members if _is_reachable(c)]


def _members_of(path: str, roles: tuple[str, ...], index: dict | None = None) -> list[dict]:
    """Active contacts in the groups of `path` that play one of `roles`, deduped."""
    index = _our_groups() if index is None else index
    seen: dict[str, dict] = {}
    for role, name in index.get(path, []):
        if role in roles:
            for contact in team_members(name):
                if contact.get("uuid"):
                    seen.setdefault(contact["uuid"], contact)
    return list(seen.values())


def node_listing(group_name: str) -> list[dict]:
    """Everyone published under a node: its `Team:` members plus its `Directory:` entries.

    For a mission-wide node, only the level's posts whose `scope` is mission.
    """
    listing = _members_of(node_path(group_name), ("team", "directory"))
    if is_mission_node(group_name):
        listing = [c for c in listing if _field(c, "scope").casefold() == MISSION_SCOPE]
    return listing


def node_recipients(group_name: str) -> list[dict]:
    """Everyone entitled to a node's directory — the people the notifier tells.

    That is the holders of a `Team:`/`Access:` grant on the node itself, on any
    node above it, or on any node below it (see `sees`).
    """
    node = node_path(group_name)
    index = _our_groups()
    seen: dict[str, dict] = {}
    if not is_mission_node(group_name):
        for path in index:
            if sees(path, node):
                for contact in _members_of(path, ("team", "access"), index):
                    seen.setdefault(contact["uuid"], contact)
        return list(seen.values())
    # Mission-wide posts go to whoever sees the level's parent but not the level
    # itself — a person who sees the whole level already gets these posts there.
    parent = _parent(node)
    for path in index:
        if sees(path, parent):
            for contact in _members_of(path, ("team", "access"), index):
                grants = grant_paths(contact)
                if (any(sees(g, parent) for g in grants)
                        and not any(sees(g, node) for g in grants)):
                    seen.setdefault(contact["uuid"], contact)
    return list(seen.values())


def collect_contacts(group_name: str, exclude_uuid: str = "") -> list[Contact]:
    """The directory for one node, as vCard-ready contacts."""
    label = node_path(group_name)
    return [
        _to_contact(c, label)
        for c in node_listing(group_name)
        if c.get("uuid") and c.get("uuid") != exclude_uuid
    ]


def find_contact(urn: str) -> dict | None:
    """Resolve a caller URN to their RapidPro contact.

    Tries the URN as given, then the alternate phone scheme — inbound WhatsApp
    creates contacts under `whatsapp:` while roster syncs write `tel:`, so a
    caller can legitimately be found under either (same reasoning as
    person_sync._sync_contact).
    """
    from app.graph.tools.rapidpro import _rp_api

    scheme, _, value = (urn or "").partition(":")
    if not value:
        return None
    digits = re.sub(r"\D", "", value)

    candidates = [urn]
    if scheme in ("whatsapp", "tel"):
        candidates += [f"whatsapp:{digits}", f"tel:+{digits}", f"tel:{digits}"]

    tried: set[str] = set()
    for candidate in candidates:
        if candidate in tried:
            continue
        tried.add(candidate)
        data = _rp_api("GET", "contacts.json", params={"urn": candidate}, **_RP_AUTH)
        if "error" in data:
            continue
        results = data.get("results") or []
        if results:
            return results[0]
    return None


def caller_teams(contact: dict | None) -> list[str]:
    """Team groups this contact belongs to. Fail-closed: unknown caller → []."""
    if not contact or not _is_reachable(contact):
        return []
    return sorted(
        g["name"]
        for g in contact.get("groups") or []
        if (g.get("name") or "").startswith(TEAM_PREFIX)
    )


# ── Delivery ─────────────────────────────────────────────────────────────────


def _run_async(coro, timeout: float = 30.0) -> bool:
    """Run one coroutine from this sync handler.

    A dedicated thread guarantees no event loop is already running here,
    whichever way the handler was invoked (same approach as
    rapidpro.send_crm_help).
    """
    import asyncio
    import concurrent.futures

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return bool(pool.submit(lambda: asyncio.run(coro)).result(timeout=timeout))
    except Exception as exc:
        logger.warning(f"[contact_sync] wuzapi call failed: {exc}")
        return False


def deliver_vcf_whatsapp(phone: str, vcf: str, filename: str) -> bool:
    """Send the .vcf as a real WhatsApp document, bypassing RapidPro.

    Attachments cannot ride the WhatsApp channel through RapidPro — courier
    posts the wrong payload key to wuzapi and the message is dropped entirely —
    so this talks to wuzapi directly, the way send_reaction already does.
    """
    from app.api.middleware.wuzapi_client import send_document

    digits = re.sub(r"\D", "", phone)
    if not digits:
        return False
    payload = base64.b64encode(vcf.encode("utf-8")).decode("ascii")
    return _run_async(
        send_document(channel=DIRECTORY_CHANNEL, phone=digits, document_b64=f"data:text/vcard;base64,{payload}",
                      filename=filename, mime_type="text/x-vcard")
    )


def deliver_card_whatsapp(phone: str, name: str, vcard: str) -> bool:
    """Send one contact as a native, tappable WhatsApp contact card."""
    from app.api.middleware.wuzapi_client import send_contact_card

    digits = re.sub(r"\D", "", phone)
    if not digits:
        return False
    return _run_async(send_contact_card(phone=digits, name=name, vcard=vcard, channel=DIRECTORY_CHANNEL))


def deliver_vcf(to_email: str, subject: str, body: str, vcf: str, filename: str) -> bool:
    """Email the .vcf as a real MIME attachment via the email bridge.

    The bridge's /send-file endpoint is the only channel on this box that
    delivers a genuine attachment; `_send_email_via_bridge` already wraps it.
    """
    from app.graph.tools.email import _send_email_via_bridge

    tmpdir = tempfile.mkdtemp(prefix="contact-sync-")
    path = os.path.join(tmpdir, filename)
    try:
        # newline="" so the generator's CRLF endings survive to the attachment
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(vcf)
        return _send_email_via_bridge(to_email, subject, body, attachments=[path])
    except OSError as exc:
        logger.error(f"[contact_sync] VCARD_WRITE_FAILED {exc}")
        return False
    finally:
        try:
            os.unlink(path)
            os.rmdir(tmpdir)
        except OSError:
            pass


def whatsapp_files_enabled() -> bool:
    """Whether a file can actually be sent over WhatsApp right now.

    Without a token for the active WhatsApp channel every attempt fails, so a
    phone-only member has no usable channel at all — that is a configuration
    gap to report, not a transient failure to retry forever.
    """
    from app.api.middleware.wuzapi_token import wuzapi_token_available

    return wuzapi_token_available(DIRECTORY_CHANNEL)


def has_written(member: dict) -> bool:
    """Whether this contact has ever sent us a message (RapidPro's last_seen_on)."""
    return bool((member or {}).get("last_seen_on"))


def whatsapp_push_allowed(member: dict) -> bool:
    """May an unsolicited update reach this member by WhatsApp? See PUSH_ONLY_AFTER_CONTACT."""
    return not PUSH_ONLY_AFTER_CONTACT or has_written(member)


def can_reach(member: dict, whatsapp_ok: bool = True) -> bool:
    """True if this member has any usable channel in the current configuration."""
    return bool(_email_of(member or {})) or (
        whatsapp_ok and bool(_phone_of(member or {})) and whatsapp_files_enabled()
    )


def deliver_text_whatsapp(phone: str, text: str) -> bool:
    """Plain WhatsApp text via RapidPro. Text (unlike media) rides this path fine."""
    from app.graph.tools.rapidpro import _rp_api

    digits = re.sub(r"\D", "", phone)
    if not digits:
        return False
    result = _rp_api("POST", "broadcasts.json", **_RP_AUTH,
                     json={"urns": [f"whatsapp:{digits}"], "text": text[:4000]})
    return "error" not in result


def _requester_first(phones: tuple[str, ...], urn: str) -> tuple[str, ...]:
    """Put the number a request came from first, if it is one of the contact's own.

    A post often has two SIMs and WhatsApp may be on either one. The request
    proves which one has it; the first number on the record is only a guess.
    Nothing outside the record is ever used, so this can reorder, never redirect.
    """
    digits = re.sub(r"\D", "", (urn or "").partition(":")[2])
    first = [p for p in phones if digits and re.sub(r"\D", "", p) == digits]
    return tuple(first + [p for p in phones if p not in first])


def deliver_to_member(member: dict, vcf: str, filename: str, subject: str,
                      body: str, prefer: str = "whatsapp", via_urn: str = "",
                      whatsapp_ok: bool = True) -> tuple[bool, str]:
    """Deliver an update to one member on their own channel, with a fallback.

    The single delivery path shared by the keyword handler and the notifier job.
    Returns (ok, via) where via is "whatsapp", "email", or "" if nothing worked.
    Addresses always come from the contact record, never from caller input.

    An empty `vcf` means there is nothing to import — a removal advisory, say —
    so it goes out as plain text rather than as an empty attachment. `via_urn`
    is the address a request came from, tried first when it is on the record.
    """
    phones = _requester_first(_phone_of(member or {}), via_urn)
    email = _email_of(member or {})
    order = ("email", "whatsapp") if prefer == "email" else ("whatsapp", "email")
    has_file = bool(vcf.strip())

    for channel in order:
        if channel == "whatsapp" and phones and whatsapp_ok:
            ok = (deliver_vcf_whatsapp(phones[0], vcf, filename) if has_file
                  else deliver_text_whatsapp(phones[0], body))
            if ok:
                return True, "whatsapp"
        if channel == "email" and email:
            ok = (deliver_vcf(email, subject, body, vcf, filename) if has_file
                  else _send_text_email(email, subject, body))
            if ok:
                return True, "email"
    return False, ""


def _send_text_email(to_email: str, subject: str, body: str) -> bool:
    from app.graph.tools.email import _send_email_via_bridge

    return _send_email_via_bridge(to_email, subject, body)


def notify_member_text(member: dict, subject: str, text: str,
                       prefer: str = "whatsapp", via_urn: str = "") -> bool:
    """Best-effort plain-text note to a member on their own channel."""
    phones = _requester_first(_phone_of(member or {}), via_urn)
    email = _email_of(member or {})
    order = ("email", "whatsapp") if prefer == "email" else ("whatsapp", "email")
    for channel in order:
        if channel == "whatsapp" and phones and deliver_text_whatsapp(phones[0], text):
            return True
        if channel == "email" and email and _send_text_email(email, subject, text):
            return True
    return False


# ── Tool entry point ─────────────────────────────────────────────────────────


# ── Reading a request ────────────────────────────────────────────────────────

_ALL_WORDS = {"all", "tout", "tou", "everything", "full"}
_SYNC_WORDS = {"sync", "kontak", "contacts"}
_MENU_WORDS = {"menu", "meni", "aide", "help", "ed"}
# A thank-you after a delivery is not a request: no menu in reply to it.
_COURTESY = {"ok", "okay", "oke", "oki", "merci", "merci beaucoup", "mesi", "mesi anpil", "thanks",
             "thank you", "thx", "d'accord", "daccord", "dako", "bien", "byen", "parfait", "super",
             "cool", "top", "bon", "good", "great", "recu", "resevwa", "received"}


def _is_courtesy(text: str) -> bool:
    """An acknowledgement, an emoji or an empty message (a sticker, a photo)."""
    plain = _norm(text).strip(" .!?\u2019'")
    return not any(ch.isalnum() for ch in plain) or plain in _COURTESY
# IRFFG job family -> (menu text key, words people type for it, in en/fr/ht)
_FAMILIES = {
    "Medical & Paramedical": ("dept_medical", {"medical", "medicale", "medikal", "med", "medecine", "sante"}),
    "Logistics & Supply": ("dept_log", {"log", "logistics", "logistique", "lojistik", "supply", "appro", "approvisionnement"}),
    "HR & FIN": ("dept_admin", {"admin", "administration", "administrasyon", "hr", "rh", "fin", "finance", "finances"}),
    "Operations": ("dept_ops", {"ops", "operations", "operation", "operasyon"})
}
_ASSET_WORDS = {"vehicles", "vehicle", "vehicules", "vehicule", "machin", "cars", "car"}
# Department menu, in order: (word handed to the request reader, menu text key)
_DEPT_MENU = [("medical", "dept_medical"), ("log", "dept_log"), ("admin", "dept_admin"),
              ("ops", "dept_ops"), ("vehicles", "dept_assets")]


def _norm(text) -> str:
    """Case- and accent-insensitive form, so "Médical", "medical" and "MEDICAL" meet."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().strip()


ROW_TITLE, BUTTON_TITLE, ROW_DESC, MAX_ROWS = 24, 20, 72, 10   # WhatsApp's own limits


def _fit(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "\u2026"


def option(key: str, title: str, desc: str = "", section: str = "") -> dict:
    """One choice of a menu: what to send back, what it is called, one line more, its group."""
    return {"key": key, "title": title, "desc": desc, "section": section}


def menu_options(spec, lang: str) -> list[dict]:
    """Options from a spec of ("§", section key) and (key, title key, description key)."""
    out, section = [], ""
    for item in spec:
        if item[0] == "\u00a7":
            section = _t(item[1], lang)
            continue
        key, title_key, desc_key = item
        out.append(option(key, _t(title_key, lang), _t(desc_key, lang) if desc_key else "", section))
    return out


def menu_rows(options: list[dict]) -> list[str]:
    """The tappable form of a menu, for the flow's quick replies.

    Three choices or fewer become buttons (the title alone); more become list
    rows, each with its description and, when the channel understands it, its
    section ("Section|Title"). Titles fit WhatsApp's row and button limits."""
    if not TAPPABLE or not options:
        return []
    if len(options) <= 3:
        return [_fit(o["title"], BUTTON_TITLE) for o in options]
    rows = []
    for o in options[:MAX_ROWS]:
        title = _fit(o["title"], ROW_TITLE)
        if o["section"] and LIST_SECTIONS:
            title = f"{o['section']}|{title}"
        rows.append(title + (f"<extra>{_fit(o['desc'], ROW_DESC)}" if o["desc"] else ""))
    return rows


def menu_text(title: str, ask: str, options: list[dict], back: str = "", prompt: str = "") -> str:
    """The body of a menu. With tappable choices it is the title and one line —
    the choices are in the list. Without, the whole numbered menu, its
    sections, and how to answer."""
    if TAPPABLE:
        return f"{title}\n{ask}" if ask else title
    lines, section = [title], None
    for o in options:
        if o["section"] != section:
            section = o["section"]
            lines.append("")
            if section:
                lines.append(f"*{section}*")
        lines.append(f"{o['key']}  {o['title']}")
    if back:
        lines.append(f"0  {back}")
    if prompt:
        lines += ["", prompt]
    return "\n".join(lines)


def resolve(text: str, options: list[dict]) -> str:
    """The key chosen: a leading number, or a tapped or typed title (case and
    accents aside). "" when it is neither."""
    m = re.match(r"^\s*(\d)(?:\s|$)", text or "")
    if m:
        return m.group(1)
    typed = _norm(text)
    for o in options:
        if typed and typed in {_norm(o["title"]), _norm(_fit(o["title"], ROW_TITLE)), _norm(_fit(o["title"], BUTTON_TITLE))}:
            return o["key"]
    return ""


def choice_of(text: str) -> str:
    """A leading number, else the normalised text — for callers with no menu at hand."""
    m = re.match(r"^\s*(\d)(?:\s|$)", text or "")
    return m.group(1) if m else _norm(text)


def _aliases() -> dict[str, str]:
    """Short level names from CONTACT_SYNC_LEVEL_ALIASES: {"bld": "Org/Region/SiteB"}."""
    out = {}
    for pair in LEVEL_ALIASES.split(","):
        alias, sep, path = pair.partition("=")
        if sep and alias.strip() and path.strip():
            out[_norm(alias)] = PATH_SEP.join(part.strip() for part in path.split(PATH_SEP))
    return out


def _level_names() -> dict[str, str]:
    out = {}
    for pair in LEVEL_NAMES.split(","):
        path, sep, name = pair.partition("=")
        if sep and path.strip() and name.strip():
            out[_norm(PATH_SEP.join(part.strip() for part in path.split(PATH_SEP)))] = name.strip()
    return out


def level_name(key: str) -> str:
    """A level's display name (CONTACT_SYNC_LEVEL_NAMES), else the last part of its path."""
    path = node_path(key)
    return _level_names().get(_norm(path)) or path.rsplit(PATH_SEP, 1)[-1]


def level_display(key: str, lang: str) -> str:
    """How a node is named to a member, in their language."""
    name = level_name(key)
    return f"{name} \u00b7 {_t('mission_suffix', lang)}" if is_mission_node(key) else name


def _directory_name(grants: list[str]) -> str:
    """What to call a member's whole directory: the root(s) of their grants."""
    return " + ".join(sorted({g.split(PATH_SEP)[0] for g in grants}))


def _match_level(term: str, nodes: list[str]) -> str | None:
    """The node a term names, among `nodes` only: by alias, full path or last part."""
    wanted = _norm(term)
    target = _norm(_aliases().get(wanted, ""))
    for node in nodes:
        path = node_path(node)
        if (target and _norm(path) == target) or wanted in (
                _norm(team_label(node)), _norm(path), _norm(path.rsplit(PATH_SEP, 1)[-1]),
                _norm(level_name(node))):
            return node
    return None


def _parse_request(term: str, nodes: list[str]) -> tuple[str, str]:
    """(mode, value) for what follows `sync`.

    Modes: smart (nothing typed), all, level (a node the caller can see), family
    (an IRFFG job family), assets (vehicles and shared phones), search (anything
    else, matched against post names). A level the caller cannot see is never
    matched, so naming it falls through to a search that finds nothing: the
    reply does not reveal that it exists.
    """
    wanted = _norm(term)
    if not wanted:
        return "smart", ""
    if wanted in _ALL_WORDS:
        return "all", ""
    level = _match_level(term, nodes)
    if level:
        return "level", level
    for family, (_key, words) in _FAMILIES.items():
        if wanted in words:
            return "family", family
    if wanted in _ASSET_WORDS:
        return "assets", ""
    return "search", term.strip()


def _catalog_entry(card: Contact) -> dict:
    return (_catalog().get(card.function) or {}) if card.function else {}


def _matches(card: Contact, mode: str, value: str) -> bool:
    if mode == "family":
        return card.kind == "post" and _catalog_entry(card).get("family") == value
    if mode == "assets":
        return card.kind == "asset"
    wanted = _norm(value)
    if len(wanted) < 2:
        return False
    titles = (_catalog_entry(card).get("title") or {}).values()
    return any(wanted in _norm(text) for text in (card.name, card.alias, card.title, *titles))


def _lang_for(contact: dict | None) -> str:
    """The caller's language: their menu choice, else RapidPro's, else the default."""
    uid = (contact or {}).get("uuid", "")
    if uid:
        from app.jobs.contact_sync_notify import lang_pref

        chosen = lang_pref(uid)
        if chosen:
            return chosen
    return (contact or {}).get("language") or DEFAULT_LANG


def _identity(args: dict, kwargs: dict) -> str:
    # Who is asking comes from the authenticated session, never from the tool's
    # arguments: on the assistant's path those are written by the model, and a
    # model can be talked into naming somebody else. Arguments are only a
    # fallback for callers that run outside a session and pass an identity they
    # authenticated themselves (the notifier's tests, direct calls).
    from app.hermes.tools import _current_urn

    urn = (_current_urn.get() or "").strip()
    return urn or (args.get("user_id") or kwargs.get("user_id") or "").strip()


# ── Tool entry points ────────────────────────────────────────────────────────


def macro_sync(args: dict, **kwargs) -> str:
    """Send the caller the part of the contact directory they ask for.

    `sync` sends only what the caller is missing (everything the first time,
    only changes afterwards, nothing when they are up to date); `sync all` sends
    everything again; `sync <site>`, `sync <department>` and `sync <post>` send
    part of it. Always limited to what the caller may see: membership of a
    `Team:` or `Access:` group, matched against the tree.
    """
    urn = _identity(args, kwargs)
    requested = (args.get("_args") or [])
    requested = " ".join(requested).strip() if isinstance(requested, list) else str(requested).strip()
    requested = requested or (args.get("team") or "").strip()

    if not urn or ":" not in urn:
        logger.warning("[contact_sync] SYNC_DENIED reason=no_identity")
        return _t("denied", DEFAULT_LANG)

    contact = find_contact(urn)
    grants = grant_paths(contact)
    lang = _lang_for(contact)
    if not grants:
        logger.warning(f"[contact_sync] SYNC_DENIED urn={_mask(urn)} reason=not_on_a_team")
        return _t("denied", lang)
    return _serve(urn, contact, grants, lang, requested)


def _serve(urn: str, contact: dict, grants: list[str], lang: str, requested: str) -> str:
    """Work out what to send, answer at once, and deliver in the background."""
    teams = visible_nodes(grants)
    mode, value = _parse_request(requested, teams)
    member_uid = (contact or {}).get("uuid", "")

    # Decide the channel now, from the contact record — never from the request.
    # An inbound From: header is forgeable, so a forged request can only ever
    # deliver to the real owner of the mailbox.
    prefer = "email" if urn.partition(":")[0] == "mailto" else "whatsapp"
    phones, to_email = _phone_of(contact or {}), _email_of(contact or {})
    if not phones and not to_email:
        logger.warning(f"[contact_sync] SYNC_DENIED urn={_mask(urn)} reason=no_contact_method")
        return _t("no_email", lang)
    # Only promise WhatsApp if it can actually carry a file.
    via_whatsapp = prefer == "whatsapp" and bool(phones) and whatsapp_files_enabled()
    if not via_whatsapp and not to_email:
        logger.warning(f"[contact_sync] SYNC_DENIED urn={_mask(urn)} reason=no_email_on_file")
        return _t("no_email", lang)

    nodes = [value] if mode == "level" else teams
    try:
        listings = {node: collect_contacts(node) for node in nodes}   # complete, own card included
    except Exception:
        # Reading the directory happens before the answer now; a failure must
        # still end in a clear reply, never in an error the member cannot read.
        logger.exception(f"[contact_sync] DIRECTORY_READ_FAILED urn={_mask(urn)}")
        return _t("failed", lang)
    everyone: dict[str, Contact] = {}
    for node in nodes:
        for card in listings[node]:
            if card.uid != member_uid:
                everyone.setdefault(card.uid, card)

    removed: list[str] = []
    record, key, changes = None, "", False
    if mode == "smart":
        label = _t("whole_directory", lang)
        if not everyone:
            return _t("empty", lang, team=label)
        from app.jobs.contact_sync_notify import record_on_demand, smart_plan

        cards, removed, first_time = smart_plan(member_uid, listings)
        cards = [c for c in cards if c.uid != member_uid]
        if not cards and not removed:
            logger.info(f"[contact_sync] SYNC_UP_TO_DATE urn={_mask(urn)}")
            return _t("up_to_date", lang, team=label, n=len(everyone))
        if not cards:  # only departures: the answer itself is the whole update
            record_on_demand(member_uid, "sync", listings)
            return _t("removed_note", lang, names=", ".join(removed)).strip()
        record, key, changes = listings, "sync", not first_time
    elif mode in ("all", "level"):
        label = _t("whole_directory", lang) if mode == "all" else level_display(value, lang)
        cards, record, key = list(everyone.values()), listings, ("all" if mode == "all" else label)
    else:
        label = value if mode == "search" else _t(_FAMILIES[value][0] if mode == "family" else "dept_assets", lang)
        cards = [c for c in everyone.values() if _matches(c, mode, value)]
        if not cards:
            return _t("no_match", lang, term=label)
        key = f"find:{_norm(value)}" if mode == "search" else f"dept:{value or 'assets'}"
    stem = _directory_name(grants) if mode in ("smart", "all") else label   # the file keeps the root's name
    if not cards:
        return _t("empty", lang, team=label)

    if mode != "smart" and COOLDOWN_MINUTES > 0:
        from app.jobs.contact_sync_notify import last_on_demand

        last = last_on_demand(member_uid, key)
        age = (datetime.now(timezone.utc) - last).total_seconds() / 60 if last else None
        if age is not None and 0 <= age < COOLDOWN_MINUTES:
            logger.info(f"[contact_sync] SYNC_COOLDOWN urn={_mask(urn)} minutes={int(age)}")
            return _t("cooldown", lang, team=label, minutes=max(1, round(age)),
                      wait=max(1, round(COOLDOWN_MINUTES - age)))

    logger.info(f"[contact_sync] SYNC_AUTHORIZED urn={_mask(urn)} mode={mode} n={len(cards)}")
    _deliver_async(lambda: _send(contact, urn, lang, label, cards, removed, prefer,
                                 via_whatsapp, record, key, changes, stem=stem))
    n = len(cards)
    if via_whatsapp:
        return _t("sending_changes_here" if changes else "sending_here", lang, team=label, n=n)
    return _t("sending_changes_email" if changes else "sending_email", lang,
              team=label, n=n, email=_mask(to_email))


def _send(contact: dict, urn: str, lang: str, label: str, cards: list, removed: list[str],
          prefer: str, via_whatsapp: bool, record: dict | None, key: str, changes: bool, stem: str = "") -> None:
    """Deliver what _serve promised. Every dead end tells the member why."""
    subject = _t("subject", lang, team=label)
    member_uid = (contact or {}).get("uuid", "")
    try:
        localized = sorted(localize(cards, lang), key=lambda c: c.sort_key)
        note = _t("removed_note", lang, names=", ".join(removed)).strip() if removed else ""
        ok, via = False, ""
        remaining = list(localized)
        # A handful of contacts: native WhatsApp cards, which the phone saves in
        # one tap, with no file to open. Whatever fails as a card goes in a file,
        # and only that: the cards that arrived must not come a second time.
        if via_whatsapp and localized and len(localized) <= CARD_LIMIT:
            phone = _requester_first(_phone_of(contact or {}), urn)[0]
            remaining = [c for c in localized
                         if not deliver_card_whatsapp(phone, c.display_name, build_vcard(c))]
            ok, via = not remaining, "whatsapp cards"
            if ok and note:
                notify_member_text(contact or {}, subject, note, prefer, via_urn=urn)
        if remaining:
            names = ", ".join(c.display_name for c in remaining[:8]) + (", …" if len(remaining) > 8 else "")
            body = (_t("delta_body", lang, team=label, names=names) if changes
                    else _t("body", lang, n=len(remaining), team=label))
            if note:
                body += "\n\n" + note
            ok, via = deliver_to_member(
                contact or {}, build_vcf(remaining, rev=_rev()),
                f"{_slug(stem or label)}-{'new-' if changes else ''}contacts.vcf",
                subject, body.strip(), prefer=prefer, via_urn=urn)
        if ok:
            logger.info(f"[contact_sync] VCARD_DELIVERED urn={_mask(urn)} via={via} n={len(localized)}")
            from app.jobs.contact_sync_notify import record_on_demand, stamp_on_demand

            if record is not None:
                # So the notifier does not send this member a second copy.
                record_on_demand(member_uid, key, record)
            else:
                stamp_on_demand(member_uid, key)
            return
        logger.error(f"[contact_sync] DELIVERY_FAILED urn={_mask(urn)} n={len(localized)}")
        notify_member_text(contact or {}, subject, _t("failed", lang), prefer, via_urn=urn)
    except Exception:
        # A daemon thread that dies silently would break the promise with no
        # trace at all — always leave a log line.
        logger.exception(f"[contact_sync] DELIVERY_CRASHED urn={_mask(urn)}")


# ── The menu ─────────────────────────────────────────────────────────────────


def _welcome_once(contact: dict | None, lang: str) -> str:
    """A welcome line the first time a person reaches the menu, then never again."""
    from app.jobs.contact_sync_notify import get_pref, set_pref

    uid = (contact or {}).get("uuid", "")
    if not uid or get_pref(uid, "welcomed"):
        return ""
    set_pref(uid, "welcomed", "1")
    return _t("welcome", lang) + "\n\n"


MAIN_MENU = [
    ("\u00a7", "sec_contacts"),
    ("1", "opt_updates", "opt_updates_d"), ("2", "opt_all", "opt_all_d"), ("3", "opt_sites", "opt_sites_d"),
    ("4", "opt_depts", "opt_depts_d"), ("5", "opt_find", "opt_find_d"),
    ("\u00a7", "sec_ict"),
    ("6", "opt_ict_new", "opt_ict_new_d"), ("7", "opt_ict_mine", "opt_ict_mine_d"), ("8", "opt_dir_fix", "opt_dir_fix_d"),
    ("\u00a7", "sec_settings"),
    ("9", "opt_lang", "opt_lang_d"), ("0", "exit", None),
]
# A listed-only SIM (a post without a sync grant): requests and settings only.
ICT_MENU = [
    ("\u00a7", "sec_ict"),
    ("1", "opt_ict_new", "opt_ict_new_d"), ("2", "opt_ict_mine", "opt_ict_mine_d"), ("3", "opt_dir_fix", "opt_dir_fix_d"),
    ("\u00a7", "sec_settings"),
    ("4", "opt_lang", "opt_lang_d"), ("0", "exit", None),
]


def _site_label(node: str, lang: str) -> str:
    path = node_path(node)
    alias = next((a for a, p in _aliases().items() if _norm(p) == _norm(path)), "")
    label = level_name(node) + (f" ({alias.upper()})" if alias else "")
    return f"{label} \u00b7 {_t('mission_suffix', lang)}" if is_mission_node(node) else label


def _site_options(teams: list[str], lang: str) -> list[dict]:
    section = _t("sec_sites", lang)
    options = []
    for i, node in enumerate(teams, 1):
        alias = next((a for a, p in _aliases().items() if _norm(p) == _norm(node_path(node))), "")
        parts = [_t("mission_suffix", lang).capitalize() if is_mission_node(node) else "", alias.upper()]
        options.append(option(str(i), level_name(node), " \u00b7 ".join(x for x in parts if x), section))
    return options + [option("0", _t("back", lang), "", section)]


def _dept_options(lang: str) -> list[dict]:
    section = _t("sec_depts", lang)
    options = [option(str(i), _t(key, lang), _t(key + "_d", lang), section) for i, (_w, key) in enumerate(_DEPT_MENU, 1)]
    return options + [option("0", _t("back", lang), "", section)]


def _lang_options(lang: str) -> list[dict]:
    return [option("1", _t("lang_fr", lang)), option("2", _t("lang_ht", lang)), option("3", _t("lang_en", lang))]


def menu_button(lang: str) -> list[dict]:
    """The one button a closing reply carries: the way back to the menu."""
    return [option("menu", _t("menu_button", lang))]


def _is_back(text: str, lang: str) -> bool:
    """0, or the word for back, exit or cancel in the reader's language."""
    return resolve(text, [option("0", _t(k, lang)) for k in ("back", "exit", "cancel")]) == "0"


def _submenu(title_key: str, ask_key: str, options: list[dict], lang: str, lead: str = "") -> str:
    body = menu_text(_t(title_key, lang), _t(ask_key, lang), options, prompt=_t("prompt_number", lang))
    return f"{lead}\n\n{body}" if lead else body


def _pick(choice: str, options: list):
    return options[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(options) else None


def _is_operator(urn: str) -> bool:
    """The operator's own identities (ADMIN_PHONE: a number, an address), by the rule the other
    tools use: a number by its digits, an address as it is."""
    value = (urn or "").split(":", 1)[-1].strip().lstrip("+")
    digits = "".join(c for c in value if c.isdigit())
    for entry in (x.strip() for x in os.getenv("ADMIN_PHONE", "").split(",")):
        if not entry:
            continue
        if "@" in entry:
            if value.lower() == entry.lower():
                return True
        elif digits and digits == "".join(c for c in entry if c.isdigit()):
            return True
    return False


def _router_reply(urn: str, text: str) -> str:
    """The router's deterministic answer to a line the operator typed outside a door: the
    TalkPrep brain's reply when it knows the line, else nothing. No model is reached from here,
    and a persona switch the brain would propose is not followed."""
    try:
        import httpx
        from app.api.middleware.rivebot_client import RIVEBOT_URL

        resp = httpx.post(f"{RIVEBOT_URL}/match", json={"message": text, "persona": "talkprep", "user": urn},
                          timeout=15.0)
        data = resp.json() if resp.status_code == 200 else {}
        if data.get("matched") and not (data.get("context") or {}).get("switch_persona"):
            return str(data.get("response") or "")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[contact_sync] router pass failed: {e}")
    return ""


def macro_sync_dialog(args: dict, **kwargs) -> str:
    """One turn of the directory menu, for the RapidPro flow that relays it.

    The flow sends the member's message and the step it was given last time,
    shows the reply with its choices, and waits for the next message only
    while `next` is set. It holds no logic: every menu is built here from the
    member's own access, so a menu never offers what they may not see.
    Returns JSON {"text", "next", "ticket", "replies"}.
    """
    from app.graph.tools import ict_requests

    def out(text: str, nxt: str = "", ticket: str = "", rows: list | None = None) -> str:
        return json.dumps({"text": text, "next": nxt, "ticket": ticket, "replies": menu_rows(rows or [])},
                          ensure_ascii=False)

    urn = _identity(args, kwargs)
    text = str(args.get("text") or "").strip()
    step = str(args.get("step") or "").strip()
    ticket_uuid = str(args.get("ticket_uuid") or "").strip()
    asked = _norm(text.partition(" ")[0]) in (_SYNC_WORDS | _MENU_WORDS)
    if not urn or ":" not in urn:
        return out(_t("denied", DEFAULT_LANG) if asked else "")
    contact = find_contact(urn)
    grants = grant_paths(contact)
    listed = listed_paths(contact)
    lang = _lang_for(contact)
    if not grants and not listed:
        # Someone who asked by name is told whom to ask; anything else from a
        # stranger (the line also receives every other message) gets silence.
        logger.warning(f"[contact_sync] MENU_DENIED urn={_mask(urn)} reason=not_listed asked={asked}")
        return out(_t("denied", lang) if asked else "")
    teams = visible_nodes(grants) if grants else []
    # Listed without a sync grant (a post's SIM, say): ICT requests only.
    home = "main" if grants else "main_ict"

    def home_options(l: str) -> list[dict]:
        return _main_options(l) if grants else _ict_options(l)

    def show_home(lead: str = "", l: str = "") -> str:
        l = l or lang
        options = home_options(l)
        body = menu_text(_t("menu_title" if grants else "ict_title", l), _t("ask_main", l), options,
                         prompt=_t("prompt_number", l))
        return out(f"{lead}\n\n{body}" if lead else body, home, rows=options)

    def request(name: str) -> str:
        return out(*ict_requests.step(name, text, contact or {}, lang))

    first, _, rest = text.partition(" ")
    if _norm(first) in _SYNC_WORDS:
        if not grants:
            logger.warning(f"[contact_sync] SYNC_DENIED urn={_mask(urn)} reason=listed_only")
            return out(_t("denied", lang))
        # `sync …` typed at any point in the menu means what it always means.
        return out(_serve(urn, contact, grants, lang, rest.strip()), rows=menu_button(lang))
    if _norm(first) in _MENU_WORDS:
        return show_home(lead=_welcome_once(contact, lang).strip() if not step else "")
    if not step:
        # A number or a choice sent after the conversation closed still means
        # the menu line it names.
        picked = resolve(text, home_options(lang))
        if picked and picked != "0":
            step = home
    if not step:
        # Anything else is conversation, not a request: the line is also the
        # operator's own WhatsApp, and a colleague writing to a person must
        # never be answered by a menu. `menu`, `sync`, a number or a tapped
        # row are the only ways in; the first `menu` carries the welcome.
        # The operator alone gets one more reading: a verb the router knows
        # (the talk-preparation loop) is answered here, deterministically; the
        # model stays behind its own door.
        return out(_router_reply(urn, text) if _is_operator(urn) else "")
    if step.startswith(("ict_", "dir_")):
        if _is_back(text, lang) and not step.startswith("ict_opened"):
            return show_home()
        return out(*ict_requests.step(step, text, contact or {}, lang, ticket_uuid))
    if _is_back(text, lang):
        return out(_t("menu_bye", lang)) if step in ("main", "main_ict") else show_home()
    if step == "main_ict":
        choice = resolve(text, home_options(lang))
        actions = {"1": lambda: request("ict_start"), "2": lambda: request("ict_mine"),
                   "3": lambda: request("dir_start"),
                   "4": lambda: out(_submenu("lang_title", "ask_lang", _lang_options(lang), lang), "lang", rows=_lang_options(lang))}
        if choice in actions:
            return actions[choice]()
        if _is_courtesy(text):
            return out(_t("menu_bye", lang))
        return show_home(lead=_t("menu_invalid", lang))
    if step == "main":
        choice = resolve(text, home_options(lang))
        # A delivery closes the conversation; its reply carries a Menu button,
        # and a number or a title sent later still means the line it names.
        actions = {"1": lambda: out(_serve(urn, contact, grants, lang, ""), rows=menu_button(lang)),
                   "2": lambda: out(_serve(urn, contact, grants, lang, "all"), rows=menu_button(lang)),
                   "3": lambda: out(_submenu("sites_title", "ask_site", _site_options(teams, lang), lang), "site",
                                    rows=_site_options(teams, lang)),
                   "4": lambda: out(_submenu("depts_title", "ask_dept", _dept_options(lang), lang), "dept", rows=_dept_options(lang)),
                   "5": lambda: out(_submenu("find_title", "find_body", [], lang), "find", rows=[option("0", _t("back", lang))]),
                   "6": lambda: request("ict_start"), "7": lambda: request("ict_mine"), "8": lambda: request("dir_start"),
                   "9": lambda: out(_submenu("lang_title", "ask_lang", _lang_options(lang), lang), "lang", rows=_lang_options(lang))}
        if choice in actions:
            return actions[choice]()
        if choice.isdigit() or _norm(text).isdigit():
            return show_home(lead=_t("menu_invalid", lang))
        # Anything else while the menu is open is conversation too (a search
        # has its own line, "Rechercher un poste", and `sync <poste>`): the
        # menu closes without a word.
        return out("")
    if step == "site":
        options = _site_options(teams, lang)
        node = _pick(resolve(text, options), teams)
        if node is None:
            return out(_submenu("sites_title", "ask_site", options, lang, _t("menu_invalid", lang)), "site", rows=options)
        return out(_serve(urn, contact, grants, lang, node_path(node) + (MISSION_MARK if is_mission_node(node) else "")),
                   rows=menu_button(lang))
    if step == "dept":
        options = _dept_options(lang)
        picked = _pick(resolve(text, options), _DEPT_MENU)
        if picked is None:
            return out(_submenu("depts_title", "ask_dept", options, lang, _t("menu_invalid", lang)), "dept", rows=options)
        return out(_serve(urn, contact, grants, lang, picked[0]), rows=menu_button(lang))
    if step == "find":
        return out(_serve(urn, contact, grants, lang, text), rows=menu_button(lang))
    if step == "lang":
        options = _lang_options(lang)
        code = {"1": "fr", "2": "ht", "3": "en"}.get(resolve(text, options))
        if not code:
            return out(_submenu("lang_title", "ask_lang", options, lang, _t("menu_invalid", lang)), "lang", rows=options)
        from app.jobs.contact_sync_notify import set_lang_pref

        set_lang_pref((contact or {}).get("uuid", ""), code)
        return show_home(lead=_t("lang_set", code), l=code)
    return show_home()


def _main_options(lang: str) -> list[dict]:
    return menu_options(MAIN_MENU, lang)


def _ict_options(lang: str) -> list[dict]:
    return menu_options(ICT_MENU, lang)


def _deliver_async(fn) -> None:
    """Run delivery off the request, so the chat reply beats RiveBot's timeout."""
    if not DELIVER_IN_BACKGROUND:
        fn()
        return
    threading.Thread(target=fn, name="contact-sync-deliver", daemon=True).start()
