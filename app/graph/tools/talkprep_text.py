"""Every line the talk-preparation tools say to a person, in Kreyòl and English.

The flows carry these replies to WhatsApp as they are, so they follow the conversational
standard: plain words, no commands to type, no identifiers, next steps named the way the
menu names them. The language comes from the request (``lang``: hat/ht, eng/en, fra/fr),
else ``TALKPREP_LANG``, else Kreyòl, the personal line's language.
"""

import os

_ALIASES = {"ht": "ht", "hat": "ht", "cr": "ht", "en": "en", "eng": "en", "fr": "en", "fra": "en"}

# Publication codes as a person knows them, and the store's language ids as names.
PUBLICATIONS = {
    "ht": {"s34": "Plan diskou piblik (S-34)", "lmd": "Renmen moun, fè disip (lmd)",
           "th": "Bwochi ansèyman (th)", "scl": "Scl"},
    "en": {"s34": "Public talk outlines (S-34)", "lmd": "Love People, Make Disciples (lmd)",
           "th": "Teaching brochure (th)", "scl": "Scl"},
}
LANGUAGE_IDS = {
    "ht": {"0": "Angle", "1": "Panyòl", "3": "Franse", "51": "Kreyòl"},
    "en": {"0": "English", "1": "Spanish", "3": "French", "51": "Kreyòl"},
}

T = {
    "ht": {
        "error": "⚠️ Sa pa t mache. Eseye ankò pita.",
        # gates
        "gate_talk_missing": "Pa gen diskou sa a. Gade *Diskou mwen yo*, oswa *Ajoute yon diskou*.",
        "gate_revision_missing": "Pa gen vèsyon « {name} ». Kreye yon vèsyon anvan (Travay sou diskou › Kreye vèsyon).",
        "gate_no_draft": "Ou poko ekri okenn seksyon pou « {name} ». Ekri bouyon w nan SiYuan anvan, apre sa mande yon defi sou seksyon an. Evalyasyon an jije pwòp mo w yo.",
        "gate_no_eval": "Poko gen evalyasyon pou « {name} ». Fè evalyasyon an anvan (Travay sou diskou › Evalye diskou).",
        "gate_no_rehearsal": "Fè omwen yon repetisyon anvan w ekspòte (Travay sou diskou › Repetisyon).",
        # help
        "help": (
            "🎙️ *Preparasyon diskou*\n"
            "Ou toujou pale de yon diskou ak nimewo l, jan l ye sou plan an.\n\n"
            "*1. Kòmanse.* Prepare diskou a pou dimanch li (prepare diskou 75 pou 2026-12-27), oswa chwazi youn ki la deja.\n"
            "*2. Di mesaj santral ou.* Nan yon sèl fraz, ak mo pa w, sa diskou a ye (fil wouj la). Tout rès la depann de li. Apre sa chwazi youn oswa de leson pou w travay (leson 8 12).\n"
            "*3. Ekri nòt pa w.* Nan SiYuan, pou chak pati: premye minit la, fraz kle yo ak dènye minit la an antye; twa mo pa liy pou rès la. Nòt, pa yon diskou ekri mo pou mo.\n"
            "*4. Mande yon defi.* Antrenè a poze kesyon sou mo pa w yo; li pa janm ekri pou ou. Reponn alekri, korije, mande ankò.\n"
            "*5. Evalye.* Bay tèt ou yon nòt anvan; apre sa, m li bouyon w sou 20 leson bwochi ansèyman an, chak leson ak yon fraz ki soti nan tèks pa w.\n"
            "*6. Repete.* Pale sou nòt ou yo epi voye yon nòt vokal: m mezire tan an ak poz yo; se ou ki bay nòt livrezon an.\n"
            "*7. Ekspòte.* Rasanble maniskri w lè gen yon vrè repetisyon.\n"
            "*8. Jou apre a.* Bilan: sa k pase byen, sa k pa pase, yon nòt sou chak leson; make yon leson metrize lè w posede l; gade pwogrè w.\n\n"
            "Gid konplè: SiYuan › Life › Gid — Preparasyon Diskou."
        ),
        # status
        "no_talks": "🎙️ *Poko gen diskou.*\nAjoute youn ak nimewo plan li (Ajoute yon diskou), oswa gade piblikasyon yo.",
        "status_title": "🎙️ *Diskou ou yo*",
        "active_line": "🎙️ Diskou {handle} — {title}",
        "status_talk": "\n*Diskou {handle}* — {title}{active}",
        "active_mark": " · aktif",
        "status_no_revision": "   poko gen vèsyon",
        "status_rev": "   • {name}: {thread}, {prog}",
        "thread_set": "fil wouj la la",
        "thread_missing": "pa gen fil wouj",
        "prog_sections": "{total} seksyon",
        "prog_drafted": "{drafted}/{total} seksyon ekri",
        "status_next": "\nPou travay sou youn: Chwazi yon diskou (egzanp {handle}).",
        # select
        "select_which": "Ki diskou? Di m nimewo a, jan l ye sou plan an (egzanp 50). Gade *Diskou mwen yo* pou lis la.",
        "select_unknown": "Mwen pa gen diskou « {query} ». Gade *Diskou mwen yo*, oswa *Ajoute yon diskou*.",
        "select_ok": "✅ N ap travay sou *diskou {handle}* — {title}",
        "select_revisions": "Vèsyon yo:",
        "select_rev_line": "   • {name}{thread}",
        "select_thread_mark": " · fil wouj la la",
        "select_no_revisions": "Poko gen vèsyon.",
        "select_next_with_revs": "Apre sa: travay sou yon seksyon, oswa kreye yon lòt vèsyon pou yon lòt oditwa.",
        "select_next_no_revs": "Apre sa: kreye yon vèsyon pou oditwa a (Travay sou diskou › Kreye vèsyon).",
        "disambiguation": "🤔 Plizyè diskou koresponn ak « {query} ». Kilès? Reponn ak nimewo a.",
        "disambiguation_line": "• {selector}: {title}",
        # publications and topics
        "pubs_none": "Pa gen piblikasyon sou bwat la.",
        "pubs_title": "📚 *Piblikasyon ki disponib*",
        "pub_line": "• {name} — {langs} — {topics} sijè",
        "topics_need_pub": "Di m ki piblikasyon (egzanp S-34).",
        "topics_none": "Pa jwenn sijè pou « {pub} ».",
        "topics_title": "📋 *Sijè nan {pub}*",
        "topic_line": "• {name} — {category}",
        "topics_more": "\n_(premye 30 sou {n})_",
        "topics_next": "\n\nPou ajoute youn: Ajoute yon diskou, ak nimewo l.",
        # import
        "import_no_pub": "Pa gen piblikasyon aktif. Gade sijè yon piblikasyon anvan, oswa voye fichye .jwpub la.",
        "import_ok": "✅ *Diskou {handle} ajoute* — {title}\n{sections} seksyon, {points} pwen.\nSe li ki aktif kounye a. Apre sa: kreye yon vèsyon pou oditwa a.",
        # revision
        "rev_which_talk": "📌 Pou ki diskou? Chwazi l anvan (Chwazi yon diskou, ak nimewo a).",
        "rev_exists": "Vèsyon « {name} » egziste deja pou diskou sa a. Chwazi yon lòt non, egzanp v2.",
        "rev_canvases_ready": "\nEspas travay ou yo pare nan SiYuan: {url}\nEkri bouyon w nan chak seksyon anvan.",
        "rev_canvases_deferred": "\n⚠️ SiYuan pa t reponn; vèsyon an sove kanmenm. Eseye devlope yon seksyon pita pou kreye espas yo.",
        "rev_created": "✅ *Vèsyon « {name} » kreye* pou diskou {handle} — {title}\nOditwa: {audience}{note}\nApre sa:\n1. Di mesaj santral ou nan yon fraz (fil wouj la).\n2. Ekri bouyon w nan chak seksyon, apre sa mande yon defi.",
        "rev_not_found": "Pa jwenn vèsyon « {name} ».",
        "no_active_revision": "⚠️ Pa gen vèsyon aktif. Kreye youn anvan (Travay sou diskou › Kreye vèsyon).",
        # coaching
        "challenge": "🥊 *Defi pou « {section} »*\n\n{report}\n\n_Reponn kesyon yo nan SiYuan, fè defi awotvwa yo, korije bouyon w, apre sa mande yon lòt defi._",
        "develop_ok": "✅ *Bouyon referans pou « {section} »*\n\n_{preview}_\n\n_Konpare l ak bouyon pa w: kisa l fè ou pa t fè? Pa janm bay li jan l ye a._",
        "develop_failed": "⚠️ Mwen pa t ka fè referans pou « {section} ». Verifye non seksyon an.",
        "thread_ask": "⚠️ Di m mesaj santral ou nan YON fraz, ak mo pa w.",
        "thread_done": "🧵 *Fil wouj la fikse pou « {name} »*\n_{thread}_\n\nChak defi ap konpare bouyon w ak li. Ou ka di l pa kè, mo pou mo?",
        # evaluation
        "eval_ask_self": "🎯 Anvan m bay nòt: ki nòt ou ba tèt ou, sou 10? Aprann jije tèt ou fè pati antrènman an. Reponn ak nimewo a.",
        "eval_no_drafts": "✍️ Poko gen bouyon pa w nan SiYuan. Ekri seksyon yo anvan: m evalye mo pa w, pa mo pa m.",
        "eval_title": "📝 *Evalyasyon bouyon w (« {name} »)*",
        "eval_calibration": "Ou te di {own:.0f}/10; grily a di {ai}/10 ({delta:+.1f}: {verdict})",
        "verdict_ok": "byen kalibre 🎯",
        "verdict_high": "ou te bay tèt ou plis; etidye pwen fèb yo",
        "verdict_low": "ou te bay tèt ou mwens; fè konfyans ak fòs ou yo",
        "eval_missing": "⚠️ Poko ekri (pa evalye): {missing}",
        "eval_focus": "🎯 Travay sou pwen sa yo anvan: {points}.",
        "eval_next": "\nMande nòt yo pa leson lè w vle.",
        "scores_none": "Poko gen nòt pou « {name} ». Fè evalyasyon an anvan.",
        "scores_title": "📊 *Nòt pou « {name} »*",
        "scores_point": "• {unit} {n}, {title}: {bar} {score:.1f}/5",
        "scores_weak": "(fèb: {weak})",
        "scores_overall": "\n*Total: {overall:.1f}/5*",
        "scores_focus": "🎯 De pwen pou w travay jiska pwochen evalyasyon an: {points}.",
        # rehearsal
        "rehearsal_title": "🎤 *Repetisyon #{n}: konsèy pou « {name} »*\n\n{cues}",
        "audio_ask": "🎤 Voye m yon nòt vokal repetisyon w (oswa lyen l), epi di m ki seksyon li ye.",
        "whole_talk": "tout diskou a",
        "audio_title": "🎤 *Repetisyon analize — {label}*",
        "audio_timing": "⏱ {m}:{s:02d} sou {target} min → tan {stars} ({score}/10)",
        "audio_free": "⏱ {m}:{s:02d}",
        "audio_pauses": "⏸ {n} poz, pi long lan {longest:.1f} s",
        "audio_no_pauses": "💡 Pa gen poz ki depase 0,6 s: oditwa a bezwen silans pou kenbe pwen yo. Kote w ap mete yo?",
        "audio_long_gap": "💡 Yon twou {gap:.0f} s: yon poz ki vle di yon bagay, oswa fil la ki pèdi? Se ou ki konnen.",
        "audio_continuous": "💡 Ou pale prèske san rete; kite plas pou respire.",
        "audio_footer": "_Kounye a bay tèt ou yon nòt onèt sou livrezon an (vwa, jès, kontak je)._",
        # export, cost, deck, notebook
        # the loop: prepare, focus, draft, answer, model, rate, debrief, mastery, progress
        "prepare_ask_date": "📅 Pou ki dimanch? Bay dat la (egzanp 2026-12-27).",
        "prepare_ok": "✅ *Diskou {handle} ap prepare* — {title}\nDimanch {date}, nan {days} jou. Vèsyon « {name} ».\nApre sa: di mesaj santral ou nan yon fraz (fil wouj la), epi chwazi youn oswa de leson pou w travay.",
        "prepare_exists": "📌 Diskou {handle} pou {date} ap prepare deja (« {name} »); se li ki aktif kounye a.",
        "focus_ask": "🎯 Ki leson, youn oswa de? Di nimewo yo (1 a 20), egzanp 8 12, oswa 8-45 pou yon sou-pwen.",
        "section_ask": "🤔 Ki pati? Di nimewo l (egzanp 1), oswa yon mo nan tit li.",
        "focus_propose": "🎯 Dapre dènye bilan w, m ta pwopoze: {points}. Di nimewo ou chwazi yo.",
        "focus_bad": "⚠️ Di youn oswa de leson, 1 a 20 (egzanp 8 12). Yon sou-pwen dwe fè pati leson l (8-45).",
        "focus_set": "🎯 *Leson pou « {name} »:* {points}\n\nSa bwochi a di pou yo:\n{leads}\n\nChak defi ap gade yo anvan.",
        "draft_ask": "✍️ Di m ki pati, ak mo pou m ajoute yo.",
        "draft_ok": "✍️ Ajoute nan « {section} » ({words} mo). Pati ki ekri: {drafted}/{total}.",
        "answer_ask": "✍️ Ki kesyon, ak repons ou? Egzanp: repons 1, epi mo pa w yo.",
        "answer_none_open": "✅ Pa gen kesyon ki rete sou « {section} »; mande yon lòt defi.",
        "answer_which": "🤔 « {section} » gen {left} kesyon ki rete; di kilès, 1 a {left}.",
        "answer_ok": "✅ Repons lan sove. Rete {left} kesyon sou « {section} ».",
        "answer_ok_clear": "✅ Repons lan sove; pa gen kesyon ki rete sou « {section} ». Korije bouyon w, apre sa mande yon lòt defi, oswa tèks modèl la.",
        "model_ok": "🧪 *Tèks modèl pou « {section} »* nan jounal antrènman pati a, bò kote bouyon w. Li l fas ak pa w: kisa l fè ou pa t fè? Pa janm bay li jan l ye a.",
        "model_gate_draft": "✍️ Ekri bouyon pa w pou « {section} » anvan; tèks modèl la se pou konpare ak mo pa w.",
        "model_gate_challenge": "🥊 Mande yon defi sou « {section} » epi reponn li anvan; tèks modèl la vini apre yon defi ou fin reponn.",
        "model_gate_answers": "❓ Reponn kesyon ki rete sou « {section} » anvan; tèks modèl la ap tann repons ou yo.",
        "rate_ask": "🎤 Bay tèt ou yon nòt 1 a 5 pa pwen, egzanp: bay nòt 8=4 13=3.",
        "rate_ok": "✅ {n} nòt sove, se pa w yo:",
        "rate_unknown": "🤔 Pwen m pa konnen: {points}. Leson yo ale 1 a 20; pwen otokontwòl yo se 2, 3, 4, 8, 12, 13, 15, 16, 17, 29.",
        "audio_rate": "_Kounye a bay tèt ou yon nòt, 1 a 5 pa pwen: {points}. Reponn: bay nòt 8=4 13=3._",
        "eval_deck": "🧠 Kat memwa yo refè nan SiYuan: fil wouj la ak eskelèt la, pou w repete awotvwa.",
        "debrief_no_date": "📅 Vèsyon sa a pa gen dimanch; bilan an vini apre diskou a fin bay.",
        "debrief_too_soon": "📅 Diskou a se {date}, nan {days} jou; bilan an vini jou apre a.",
        "debrief_ask": "📝 Twa repons: sa k te pase byen, sa k pa t pase, epi 1 a 5 sou chak leson ou te chwazi. Nan chat: bilan sa k pase byen | sa k pa pase | 8=4 14=2.",
        "debrief_ok": "📝 *Bilan sove pou « {name} ».* Diskou a make kòm bay.",
        "debrief_rate_focus": "Rete pou w bay nòt, 1 a 5: {points}. Reponn: bay nòt 8=4.",
        "debrief_next": "Apre sa: make yon leson metrize lè w fin posede l (metrize 8), epi gade pwogrè w.",
        "mastered_ask": "🏁 Ki leson, 1 a 20? Egzanp: metrize 8.",
        "mastered_needs_debrief": "🏁 Metrize vini apre bilan diskou a; bay twa repons yo anvan.",
        "mastered_ok": "🏁 Make kòm metrize: {point}. Konsèy kongregasyon an ap di rès la.",
        "progress_none": "📈 Poko gen anyen. Pwogrè a kòmanse ak yon leson ou chwazi, yon evalyasyon oswa yon nòt repetisyon.",
        "progress_title": "📈 *Pwogrè w, leson pa leson*",
        "progress_focused": "chwazi {n} fwa",
        "progress_score": "tèks {score}/5",
        "progress_self": "ou menm {score}/5",
        "progress_mastered": "metrize {date}",
        "export_ok": "✅ *Maniskri a rasanble pou « {name} »*\nGade l nan SiYuan: {url}\n\nApre sa: kat memwa (Zouti etid › Kat Anki), oswa nòt nan SiYuan.",
        "export_failed": "⚠️ Mwen pa t ka rasanble maniskri « {name} ».",
        "cost": "💰 *Depans AI sesyon sa a*\nMo ki antre: {prompt:,}\nMo ki soti: {completion:,}\nTotal: {total:,}\nEstimasyon: ~${usd:.4f}",
        "deck_none": "⚠️ Pa jwenn kat pou « {what} ».",
        "deck_ok": "✅ *Kat Anki yo pare*\nKat: {n}\nPil: {name}\nTelechaje: {url}\n\nLouvri lyen an sou telefòn ou epi enpòte fichye a nan Anki. Lyen an bon pou 24 èdtan.",
        "siyuan_not_configured": "⚠️ SiYuan pa konfigire sou bwat la; nòt yo pa gen kote pou ale.",
        "siyuan_ok": "✅ *Voye nan SiYuan*\nKat: {n}\nGade: {url}",
    },
    "en": {
        "error": "⚠️ That did not work. Try again later.",
        "gate_talk_missing": "That talk is not on the box. See *My Talks*, or *Add a talk*.",
        "gate_revision_missing": "There is no version called “{name}”. Create one first (Work on talk › Create version).",
        "gate_no_draft": "You have not drafted any section of “{name}” yet. Write your draft in SiYuan first, then ask for a challenge on it. Evaluation judges your own words.",
        "gate_no_eval": "No evaluation yet for “{name}”. Evaluate first (Work on talk › Evaluate talk).",
        "gate_no_rehearsal": "Rehearse at least once before exporting (Work on talk › Rehearsal).",
        "help": (
            "🎙️ *Talk preparation*\n"
            "You always name a talk by its number, as it is on the outline.\n\n"
            "*1. Start.* Prepare the talk for its Sunday (prepare talk 75 for 2026-12-27), or choose one already there.\n"
            "*2. Say your central message.* In one sentence, in your words, what the talk is about (the thread). Everything else rests on it. Then choose one or two study points to work on (set focus 8 12).\n"
            "*3. Write your own notes.* In SiYuan, for each part: your first minute, your key sentences and your last minute in full; three words a line for the rest. Notes, never a script.\n"
            "*4. Ask for a challenge.* The coach questions your words; it never writes them. Answer in writing, revise, ask again.\n"
            "*5. Evaluate.* Give your own score first; then I read your draft on the teaching brochure's twenty study points, each with a line from your own text.\n"
            "*6. Rehearse.* Speak from your notes and send a voice note: I measure the time and the pauses; you rate the delivery yourself.\n"
            "*7. Export.* Assemble your manuscript once a real rehearsal exists.\n"
            "*8. The day after.* The debrief: what landed, what did not, a rating on each focus point; mark a point mastered once you own it; look at your progress.\n\n"
            "Full guide: SiYuan › Life › Gid — Preparasyon Diskou."
        ),
        "no_talks": "🎙️ *No talks yet.*\nAdd one by its outline number (Add a talk), or look at the publications.",
        "status_title": "🎙️ *Your talks*",
        "active_line": "🎙️ Talk {handle} — {title}",
        "status_talk": "\n*Talk {handle}* — {title}{active}",
        "active_mark": " · active",
        "status_no_revision": "   no version yet",
        "status_rev": "   • {name}: {thread}, {prog}",
        "thread_set": "thread set",
        "thread_missing": "no thread yet",
        "prog_sections": "{total} section(s)",
        "prog_drafted": "{drafted}/{total} sections drafted",
        "status_next": "\nTo work on one: Choose a talk (e.g. {handle}).",
        "select_which": "Which talk? Tell me the number as it is on the outline (e.g. 50). See *My Talks* for the list.",
        "select_unknown": "I have no talk “{query}”. See *My Talks*, or *Add a talk*.",
        "select_ok": "✅ Working on *talk {handle}* — {title}",
        "select_revisions": "Versions:",
        "select_rev_line": "   • {name}{thread}",
        "select_thread_mark": " · thread set",
        "select_no_revisions": "No versions yet.",
        "select_next_with_revs": "Next: work on a section, or create another version for another audience.",
        "select_next_no_revs": "Next: create a version for the audience (Work on talk › Create version).",
        "disambiguation": "🤔 More than one talk matches “{query}”. Which one? Reply with the number.",
        "disambiguation_line": "• {selector}: {title}",
        "pubs_none": "No publications on the box yet.",
        "pubs_title": "📚 *Publications available*",
        "pub_line": "• {name} — {langs} — {topics} topics",
        "topics_need_pub": "Tell me which publication (e.g. S-34).",
        "topics_none": "No topics found for “{pub}”.",
        "topics_title": "📋 *Topics in {pub}*",
        "topic_line": "• {name} — {category}",
        "topics_more": "\n_(first 30 of {n})_",
        "topics_next": "\n\nTo add one: Add a talk, with its number.",
        "import_no_pub": "No active publication. Browse a publication's topics first, or send the .jwpub file.",
        "import_ok": "✅ *Talk {handle} added* — {title}\n{sections} sections, {points} points.\nIt is now your active talk. Next: create a version for the audience.",
        "rev_which_talk": "📌 For which talk? Choose it first (Choose a talk, by its number).",
        "rev_exists": "Version “{name}” already exists for this talk. Pick another name, e.g. v2.",
        "rev_canvases_ready": "\nYour workspaces are ready in SiYuan: {url}\nWrite your draft in each section first.",
        "rev_canvases_deferred": "\n⚠️ SiYuan did not answer; the version is saved anyway. Develop a section later to create the workspaces.",
        "rev_created": "✅ *Version “{name}” created* for talk {handle} — {title}\nAudience: {audience}{note}\nNext:\n1. Say your central message in one sentence (the thread).\n2. Write your draft in each section, then ask for a challenge.",
        "rev_not_found": "Version “{name}” not found.",
        "no_active_revision": "⚠️ No active version. Create one first (Work on talk › Create version).",
        "challenge": "🥊 *Challenge for “{section}”*\n\n{report}\n\n_Answer the questions in SiYuan, do the out-loud challenges, revise your draft, then ask for another challenge._",
        "develop_ok": "✅ *Reference draft for “{section}”*\n\n_{preview}_\n\n_Study it against your draft: what did it do that you did not? Never deliver it as it is._",
        "develop_failed": "⚠️ I could not make the reference for “{section}”. Check the section's name.",
        "thread_ask": "⚠️ Tell me your central message in ONE sentence, in your words.",
        "thread_done": "🧵 *Thread set for “{name}”*\n_{thread}_\n\nEvery challenge will now compare your draft with it. Can you say it from memory, word for word?",
        "eval_ask_self": "🎯 Before I score it: what score do you give yourself, out of 10? Judging yourself is part of the training. Reply with the number.",
        "eval_no_drafts": "✍️ No draft of yours in SiYuan yet. Write the sections first: I evaluate your words, not mine.",
        "eval_title": "📝 *Evaluation of your draft (“{name}”)*",
        "eval_calibration": "You said {own:.0f}/10; the rubric says {ai}/10 ({delta:+.1f}: {verdict})",
        "verdict_ok": "well calibrated 🎯",
        "verdict_high": "you rated yourself higher; study the weak points",
        "verdict_low": "you rated yourself lower; trust your strengths more",
        "eval_missing": "⚠️ Not drafted yet (not scored): {missing}",
        "eval_focus": "🎯 Work on these points first: {points}.",
        "eval_next": "\nAsk for the scores by study point whenever you want.",
        "scores_none": "No scores yet for “{name}”. Evaluate first.",
        "scores_title": "📊 *Scores for “{name}”*",
        "scores_point": "• {unit} {n}, {title}: {bar} {score:.1f}/5",
        "scores_weak": "(weak: {weak})",
        "scores_overall": "\n*Overall: {overall:.1f}/5*",
        "scores_focus": "🎯 Two to work on until the next evaluation: {points}.",
        "rehearsal_title": "🎤 *Rehearsal #{n}: cues for “{name}”*\n\n{cues}",
        "audio_ask": "🎤 Send me a voice note of your rehearsal (or its link), and tell me which section it is.",
        "whole_talk": "whole talk",
        "audio_title": "🎤 *Rehearsal analysed — {label}*",
        "audio_timing": "⏱ {m}:{s:02d} against {target} min → timing {stars} ({score}/10)",
        "audio_free": "⏱ {m}:{s:02d}",
        "audio_pauses": "⏸ {n} pause(s), longest {longest:.1f} s",
        "audio_no_pauses": "💡 No pause over 0.6 s: the audience needs silence to keep the points. Where will you plant them?",
        "audio_long_gap": "💡 A {gap:.0f} s gap: a pause that means something, or a lost thread? Only you know.",
        "audio_continuous": "💡 Near-continuous speech; leave room to breathe.",
        "audio_footer": "_Now rate your delivery honestly (voice, gestures, eye contact)._",
        # the loop: prepare, focus, draft, answer, model, rate, debrief, mastery, progress
        "prepare_ask_date": "📅 For which Sunday? Give the date (e.g. 2026-12-27).",
        "prepare_ok": "✅ *Talk {handle} is being prepared* — {title}\nSunday {date}, in {days} days. Version “{name}”.\nNext: say your central message in one sentence (the thread), then choose one or two study points to work on.",
        "prepare_exists": "📌 Talk {handle} for {date} is already being prepared (“{name}”); it is your active talk now.",
        "focus_ask": "🎯 Which study points, one or two? Say their numbers (1 to 20), e.g. 8 12, or 8-45 for one sub-criterion.",
        "section_ask": "🤔 Which part? Say its number (e.g. 1), or a word of its title.",
        "focus_propose": "🎯 From your last debrief, I would propose: {points}. Say the numbers you choose.",
        "focus_bad": "⚠️ Name one or two study points, 1 to 20 (e.g. 8 12). A sub-criterion must belong to its point (8-45).",
        "focus_set": "🎯 *Focus for “{name}”:* {points}\n\nThe brochure's own leads for them:\n{leads}\n\nEvery challenge weighs these first.",
        "draft_ask": "✍️ Tell me the part, and the words to add.",
        "draft_ok": "✍️ Added to “{section}” ({words} words). Parts drafted: {drafted}/{total}.",
        "answer_ask": "✍️ Which question, and your answer? E.g. answer 1, then your words.",
        "answer_none_open": "✅ No open question on “{section}”; ask for the next challenge.",
        "answer_which": "🤔 “{section}” has {left} open question(s); say which one, 1 to {left}.",
        "answer_ok": "✅ Answered. {left} open question(s) left on “{section}”.",
        "answer_ok_clear": "✅ Answered; nothing left open on “{section}”. Revise your draft, then ask for another challenge, or the model text.",
        "model_ok": "🧪 *Model text for “{section}”* is in the part's coaching log, beside your draft. Read it against yours: what did it do that you did not? Never deliver it.",
        "model_gate_draft": "✍️ Write your own draft of “{section}” first; the model text is a comparison for your words.",
        "model_gate_challenge": "🥊 Ask for a challenge on “{section}” and answer it first; the model text comes after one answered round.",
        "model_gate_answers": "❓ Answer the open questions on “{section}” first; the model text waits for your answers.",
        "rate_ask": "🎤 Rate yourself 1 to 5 a point, e.g. rate 8=4 13=3.",
        "rate_ok": "✅ {n} rating(s) kept, yours:",
        "rate_unknown": "🤔 Not a point I know: {points}. Study points run 1 to 20; the self-check points are 2, 3, 4, 8, 12, 13, 15, 16, 17, 29.",
        "audio_rate": "_Now rate yourself, 1 to 5 a point: {points}. Reply: rate 8=4 13=3._",
        "eval_deck": "🧠 Memory deck rebuilt in SiYuan: the thread and the skeleton, to recall out loud.",
        "debrief_no_date": "📅 This version has no Sunday; the debrief comes after the talk is given.",
        "debrief_too_soon": "📅 The talk is on {date}, in {days} days; the debrief comes the day after.",
        "debrief_ask": "📝 Three answers: what landed, what did not, and 1 to 5 on each focus point. In chat: debrief what landed | what did not | 8=4 14=2.",
        "debrief_ok": "📝 *Debrief kept for “{name}”.* The talk is marked given.",
        "debrief_rate_focus": "Still to rate, 1 to 5: {points}. Reply: rate 8=4.",
        "debrief_next": "Next: mark a point mastered once you own it (mark mastered 8), and look at your progress.",
        "mastered_ask": "🏁 Which study point, 1 to 20? E.g. mark mastered 8.",
        "mastered_needs_debrief": "🏁 Mastery comes after the debrief of the talk; give the three answers first.",
        "mastered_ok": "🏁 Marked mastered: {point}. The congregation's counsel will say the rest.",
        "progress_none": "📈 No record yet. Progress starts with a focus, an evaluation or a rehearsal rating.",
        "progress_title": "📈 *Your progress, by study point*",
        "progress_focused": "focus {n}×",
        "progress_score": "text {score}/5",
        "progress_self": "yourself {score}/5",
        "progress_mastered": "mastered {date}",
        "export_ok": "✅ *Manuscript assembled for “{name}”*\nSee it in SiYuan: {url}\n\nNext: memory cards (Study tools › Anki deck), or notes in SiYuan.",
        "export_failed": "⚠️ I could not assemble the manuscript for “{name}”.",
        "cost": "💰 *AI spending this session*\nWords in: {prompt:,}\nWords out: {completion:,}\nTotal: {total:,}\nEstimate: ~${usd:.4f}",
        "deck_none": "⚠️ No cards found for “{what}”.",
        "deck_ok": "✅ *Anki deck ready*\nCards: {n}\nDeck: {name}\nDownload: {url}\n\nOpen the link on your phone and import the file into Anki. The link is good for 24 hours.",
        "siyuan_not_configured": "⚠️ SiYuan is not configured on the box; the notes have nowhere to go.",
        "siyuan_ok": "✅ *Sent to SiYuan*\nCards: {n}\nSee: {url}",
    },
}


def _default_lang() -> str:
    """The operator's setting (settings.json, then TALKPREP_LANG), else Kreyòl."""
    try:
        from app.graph.tools.settings import get_setting
        value = get_setting("reply_language")
    except Exception:
        value = None
    return str(value or os.getenv("TALKPREP_LANG", "") or "ht")


def lang_of(args: dict | None) -> str:
    raw = str((args or {}).get("lang") or _default_lang()).strip().lower()
    return _ALIASES.get(raw, "ht")


def t(key: str, args: dict | None = None, **fmt) -> str:
    """The line for ``key`` in the caller's language, formatted."""
    lang = lang_of(args)
    text = T.get(lang, T["ht"]).get(key) or T["ht"][key]
    return text.format(**fmt) if fmt else text


def publication_name(code: str, args: dict | None = None) -> str:
    code = (code or "").strip().lower()
    return PUBLICATIONS.get(lang_of(args), PUBLICATIONS["ht"]).get(code) or code.upper()


def language_name(language_id, args: dict | None = None) -> str:
    key = str(language_id)
    return LANGUAGE_IDS.get(lang_of(args), LANGUAGE_IDS["ht"]).get(key) or key
