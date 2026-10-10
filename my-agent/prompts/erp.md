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

1. Log in. For each requirement, search the ERP's material catalogue for its Kataloggruppe (narrow the search to at
   most 20 results). Add to `materials` every candidate listed: material number, description, status (active,
   blocked, ...), bore, stroke, pressure, mounting, certification, which requirement it was found for
   (`found_for`) and whether its attributes match that requirement (`matches_requirement`: true or false).
2. For every candidate whose attributes match a requirement or nearly match, open its details and add: all price
   conditions (validity from/to exactly as shown, currency, price unit, every quantity scale with its price, FX
   key), stock, lead time from stock and production lead time in weeks, pieces per carton, masses (surcharge mass,
   allocation mass) and anything else listed about it.

Then finish. Do not open records again that you have already saved.

Save as: {"materials": [...], "notes": "..."}
Pass exactly that JSON as `facts`: the top-level key is `materials` (a list, one entry per candidate), not a wrapper
or a key per search. Save the candidates of a search right away, before opening any details.
