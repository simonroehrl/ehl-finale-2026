Enter the already calculated bid on the tender portal and sign it. You choose the controls. The program types every bid amount: use value_key, never type a price yourself.

Reply with one JSON object:
{"op":"click|fill|press|scroll|back|wait|extract_tan|done","target":0,"text":"","value_key":"","format":"auto","browser":"tender|mail"}

Rules:
- One action. target is the i of a listed control on the active browser.
- browser switches which page you see next (tender portal or webmail).
- fill during submission must set value_key to one of the listed keys. format is de for a German amount, plain for an integer or a dotted decimal the field asks for.
- extract_tan: the mail view shows the signing TAN. The program reads it. Use this only when the tender form is ready for the TAN.
- done only when the tender portal itself shows that the bid was signed and accepted. Put a short phrase copied from that confirmation into text.
- If a cookie wall, popup or login blocks the page, deal with that first using the credentials in the observation (fill.text is allowed for logins only).
- Never write a click loop. If a control does nothing, pick another.
- Go straight to bid submission. Request the TAN only when everything else is entered. Enter the TAN within five minutes and confirm the binding-offer dialog the brief describes.
