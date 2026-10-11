## Your task: the tender (website `tender-portal`, browser `tender`)

Website: {SITE_URL} · username `{USERNAME}` · password `{PASSWORD}`

<brief>
{BRIEF}
</brief>

Work through these steps in order. Open each page; code reads the tables. Call `save_facts` only for a short note
if something is still missing.

1. Log in, open the tender named in the brief. Save `tender`: reference, title, buyer, deadline.
2. Open its bill of quantities (Leistungsverzeichnis). Save `positions`: for every position its number,
   description, quantity, unit and the requirement it refers to.
3. Open the bidder Q&A (Bieterfragen / Klarstellungen) and go through every page. Save `clarifications`: for every
   answer its number, date and time exactly as shown, the full answer text, what it changes in short (quantity of
   which position, technical attribute of which revision and requirement, setup fee, assigned keys such as
   Konditionsgruppe, Verpackungstarif, Terminschlüssel, Rüstpool, Bonusplan) and which earlier answer it withdraws.
4. Save `documents`: the names of the attached documents and what each contains, if it adds anything new.

Then finish. Do not read pages again that you have already saved.

Save as: {"tender": {...}, "positions": [...], "clarifications": [...], "documents": [...], "notes": "..."}
