## Your task: the technical specification (website `customer-portal`, browser `spec`)

Website: {SITE_URL} · username `{USERNAME}` · password `{PASSWORD}`

<brief>
{BRIEF}
</brief>

Work through these steps in order. After each step, call `save_facts` with the complete JSON so far.

1. Log in and find the buyer's technical specification for the tender in the brief. The document register may have
   several pages. A register row is not the document: open each revision. Opening it may need a read permission or
   acknowledgement: choose the option for binding documents, never preview or archive copies.
2. Save `revisions`: every revision with its id, date exactly as shown with its format hint, and the marking words
   printed on that opened document (do not infer current from the date or list order). Save `current_revision` only
   when a document itself is marked current or valid.
3. For the current revision go through every page of its viewer. Save `requirements`: for each requirement all
   attributes as shown (bore, stroke, pressure, mounting, certification, pieces per set, Kataloggruppe, any
   usable-stroke note), values with their locale hints, and the full original wording. Record every Kataloggruppe
   name. Do not look materials up — another agent searches the catalogue.

Then finish. Do not read pages again that you have already saved. Stay on this customer portal.

Save as: {"document": {...}, "revisions": [...], "current_revision": "...", "requirements": [...], "notes": "..."}
