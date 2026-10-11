Read only the task brief. Extract the case fields the brief itself states. Do not use website knowledge.

Return one JSON object:
{"tender_reference":"","scenario_date":"","bidder_mail_domain":"","authorized_senders":null}

scenario_date is ISO 8601 with a timezone. bidder_mail_domain is the domain after @ of the bidder mailbox. authorized_senders is null unless the brief names different local-parts for different message kinds.
