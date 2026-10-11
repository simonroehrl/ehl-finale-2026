From the run brief, copy only the live case facts the brief itself states for this run.

Return one JSON object:
{"case": {"tender_reference": null, "scenario_date": null, "bidder_mail_domain": null, "authorized_senders": null}}

Leave a field null when the brief does not state it. Do not copy an example, a placeholder or a rule. authorized_senders is an object of message kind to mailbox local-part, and only when the brief replaces the default senders. scenario_date is ISO 8601 and keeps its timezone.
