## Your task: enter and sign the bid (browsers `tender` and `mail`)

Tender portal: {TENDER_URL} · username `{TENDER_USERNAME}` · password `{TENDER_PASSWORD}`
Webmail: {MAIL_URL} · username `{MAIL_USERNAME}` · password `{MAIL_PASSWORD}`

How to submit, from the task brief:

<submission>
{BRIEF}
</submission>

The bid is calculated. Enter exactly these values, nothing else:

<bid>
{BID}
</bid>

Everything is researched already. Go straight to this tender's bid form. Once that form is open, stay on it: do
not open documents, questions, the homepage or a second tab for reading. Never click a back button: it clears
later steps. Close dialogs with `browser_handle_dialog` (accept). Prefer snapshots over evaluate.

Follow the submission steps in the `tender` browser: express interest if needed, register the bidder as the brief
says, choose the offer type for this decision, complete the configurator for every priced position (omit the
unfinished ones listed above), enter prices and the total in German number format as written above, acknowledge
the tender documents. Use the net values above. If the form's material list for a position does not contain the
number in the bid, pick the listed material that matches the attributes you just configured — do not leave the
form to look it up.

Then request the signing TAN, open the newest TAN mail in the `mail` browser (reload the inbox until it arrives;
a new request invalidates the previous TAN, each is valid for five minutes), enter it, review the summary and give
the bid with "Angebot verbindlich abgeben", confirming the browser dialog.

Request the TAN only when everything else is entered, then sign within its five minutes. Do not call `finish`
without a confirmation number from the portal. When the portal confirms the submission, call `save_facts` with
{"confirmation": "<what the portal shows>"} and `finish`.
