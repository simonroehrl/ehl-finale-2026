## Your task: the sales mailbox (website `mail`, browser `mail`)

Website: {SITE_URL} · username `{USERNAME}` · password `{PASSWORD}`

<brief>
{BRIEF}
</brief>

Work through these steps in order. After each step, call `save_facts` with the complete JSON so far.

1. Log in. Save `mailbox_domain`: the domain of your own mailbox address.
2. Search for the tender reference from the brief (search combines words with AND), in the inbox and in the
   archive, through every result page. Open each mail. Save `mails`: for each its folder, sender address, date and
   time exactly as shown, subject, the tender reference it names, and the full body text (tables included).
3. Search again with the reference plus each topic: Rabatt, Freigabe, Konditionsregeln, Technikfreigabe,
   Verpackungstarife, Terminfreigabe, Devisenfixing, Rüstpools, Projektbonus, Preisindizes. Add any mail not saved
   yet. Also add mails for similar-looking references, marked as such in a `note` field.

Then finish. Do not open mails again that you have already saved.

Save as: {"mailbox_domain": "...", "mails": [...], "notes": "..."}
