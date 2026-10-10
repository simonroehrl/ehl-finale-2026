## Your task: the technical specification (website `customer-portal`, browser `spec`)

Website: {SITE_URL} · username `{USERNAME}` · password `{PASSWORD}`

<brief>
{BRIEF}
</brief>

Work through these steps in order. After each step, call `save_facts` with the complete JSON so far.

1. Log in and find the buyer's technical specification (Lastenheft) for the tender in the brief; the document
   register may have several pages. Opening it may need a read permission or acknowledgement: choose the option for
   binding documents, never preview or archive copies, and follow its instructions.
2. Save `revisions`: every revision listed with its id, date exactly as shown with its format hint, status, change
   note, and the marking each revision shows when opened (for example "gültig" or "ersetzt"). Save
   `current_revision`: the one marked current or valid.
3. Open the current revision and go through every page of its viewer ("Nächste Seite", "Weitere laden"). Save
   `requirements`: for each requirement (A-1, A-2, ...) all attributes as shown (bore, stroke, pressure, mounting,
   certification, pieces per set, Kataloggruppe, any usable-stroke / nutzbarer Weg note), values with their
   locale hints, and the full original wording (expand "Originalwortlaut").

Then finish. Do not read pages again that you have already saved.

Save as: {"document": {...}, "revisions": [...], "current_revision": "...", "requirements": [...], "notes": "..."}
