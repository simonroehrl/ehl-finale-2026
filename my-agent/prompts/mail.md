## Your task: the sales mailbox (website `mail`, browser `mail`)

Website: {SITE_URL} · username `{USERNAME}` · password `{PASSWORD}`

<brief>
{BRIEF}
</brief>

Work through these steps in order. After each step, call `save_facts` with the complete JSON so far. If a page or
tool times out, continue from the last save; do not stop.

1. Log in. Save `mailbox_domain`: the domain of your own mailbox address.
2. Search for the tender reference from the brief (search combines words with AND), in the inbox and in the
   archive, through every result page. Open each mail once. Save `mails`: for each its folder, sender address,
   date and time exactly as shown, subject, the tender reference it names, and the full body text (tables included).
   Also open a mail whose subject is similar if the reference is almost the same; mark those in a `note` field.

Then finish. Do not open a mail again. Do not start extra topic searches — one pass per folder is enough.

Save as: {"mailbox_domain": "...", "mails": [...], "notes": "..."}
