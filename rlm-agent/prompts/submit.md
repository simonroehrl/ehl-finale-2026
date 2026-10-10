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

Everything is researched already: do not open or read the tender documents. Go straight to the bid submission
(Angebotsabgabe) of this tender. Never click a back button ("Zurück"): it clears the later steps. Close dialogs,
surveys and banners that block the page. Follow the submission steps above in the `tender` browser: express interest in the tender if
needed, register the bidder as the brief says, choose the offer type for this decision, complete the configurator
for every position, enter prices and the total in German number format (as written above), acknowledge the tender
documents. Optional fields that the bid lists must be entered too. Use the net values above; the gross amount
shown includes VAT.

Then request the signing TAN, open the newest TAN mail in the `mail` browser (reload the inbox until it arrives;
a new request invalidates the previous TAN, each is valid for five minutes), enter it, review the summary and give
the bid with "Angebot verbindlich abgeben", confirming the browser dialog.

Request the TAN only when everything else is entered, then sign within its five minutes. Work steadily: going back clears later steps, and unsaved entries are lost when the session expires. When the
portal confirms the submission (a receipt or confirmation number), call `save_facts` with {"confirmation": "<what the portal shows>"} and
`finish`.
