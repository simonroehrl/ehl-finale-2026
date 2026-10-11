## Enter and sign the bid (browsers `tender` and `mail`)

Tender: {TENDER_URL} · `{TENDER_USERNAME}` / `{TENDER_PASSWORD}`
Mail: {MAIL_URL} · `{MAIL_USERNAME}` / `{MAIL_PASSWORD}`

Follow the brief's submission section for steps and labels. Do not re-read tender documents. Go straight
to bid submission. Never use a back button: it clears later steps.

The mail username `{MAIL_USERNAME}` is the bidder contact e-mail. Type it into any e-mail field on the
tender site. Open the mail browser only for the signing TAN. A new TAN request invalidates the previous one.
If the mail page is blank, wait and open {MAIL_URL} again.

<submission>
{BRIEF}
</submission>

Enter exactly these values (German number format as written).

<bid>
{BID}
</bid>

Confirm the binding submit action the brief names, including the browser dialog.

When the portal shows a receipt, `save_facts` with `{"submitted": true, "confirmation": "<receipt>"}` and
`finish`. Do not finish while `submitted` is false.
