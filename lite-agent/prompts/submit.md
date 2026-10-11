## Enter and sign the bid (browsers `tender` and `mail`)

Tender: {TENDER_URL} · `{TENDER_USERNAME}` / `{TENDER_PASSWORD}`
Mail: {MAIL_URL} · `{MAIL_USERNAME}` / `{MAIL_PASSWORD}`

Follow the brief's submission section for steps and labels (they may differ from other sites). Do not re-read
tender documents. The browsers may already be logged in: do not open the site home. From the current page go
straight to bid submission. Log in only if a login form is visible. Never use a back button: it clears later steps.

The mail username `{MAIL_USERNAME}` is the bidder contact e-mail. Type it into any e-mail field on the tender
site. Do not open the mail browser to discover that address.

Open the mail browser only when you need the signing TAN, after every other field is entered. A new TAN
request invalidates the previous one. If the mail page is blank, wait and navigate to {MAIL_URL} again, then
log in with the mail credentials above.

<submission>
{BRIEF}
</submission>

Enter exactly these values (German number format as written). A Decision of no_bid is still a portal
submission (the brief's waiver / decline path), not a reason to stop.

<bid>
{BID}
</bid>

Confirm the binding submit action the brief names, including the browser dialog.

When the portal shows a receipt, `save_facts` with `{"submitted": true, "confirmation": "<receipt>"}` and
`finish`. Do not finish while `submitted` is false.
