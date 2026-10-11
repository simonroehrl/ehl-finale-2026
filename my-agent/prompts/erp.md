## Your task: the ERP (website `erp`, browser `erp`)

Website: {SITE_URL} · username `{USERNAME}` · password `{PASSWORD}`

<brief>
{BRIEF}
</brief>

What the specification and the tender require (collected by colleagues):

<requirements>
{REQUIREMENTS}
</requirements>

Work through these steps in order. After each requirement, call `save_facts` with the complete JSON so far. The
session times out quickly: log in again when needed.

0. Before any search, copy every Kataloggruppe named in the requirements JSON into `catalog_groups` and set
   `searched_groups` to []. Call `save_facts`. You cannot finish until every named group has been searched.
1. Log in. Search the ERP's material catalogue once per Kataloggruppe (narrow each search to at most 20 results).
   After each search, add that group to `searched_groups` even if the result is empty. Add to `materials` every
   candidate listed: material number, description, status (active, blocked, ...), bore, stroke, pressure, mounting,
   certification, the Kataloggruppe, and which requirement it was found for.
2. For every candidate whose attributes match a requirement or nearly match, open its details once and add: all
   price conditions (validity from/to exactly as shown, currency, price unit, every quantity scale with its price,
   FX key), stock, lead time from stock and production lead time in weeks, pieces per carton, masses (surcharge
   mass, allocation mass) and anything else listed about it. Never reopen a material number already in `materials`.

Then finish. Do not finish after the first group.

Save as: {"catalog_groups": [...], "searched_groups": [...], "materials": [...], "notes": "..."}
