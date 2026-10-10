# Answers and scoring

> In this document, `$API` is https://ehl-challenge.atira.ai (the CLI and API guide call it `$ATIRA_API`).

Each challenge defines its answer format and how quality is scored; the platform measures cost and time and
ranks everyone the same way. Test runs, scored submissions and the scouting logins use the public dataset (the
websites as you see them, with scenario `public-1`), and the leaderboard ranks those results. The correct answer
isn't published; your system has to work it out from the websites. When the hackathon ends, a [final run](#final-run)
on a private dataset weighs most when we pick the [winners](#winners).

## Per run

- **Quality** (0 to 100): how right the answer is, by the challenge's scorer.
- **Eligible:** whether the answer meets the challenge's mandatory criteria. For RFQ: the right tender reference;
  a wrong decision (bid or no_bid) costs quality points instead.
- **Cost:** the run's LLM usage, priced per model, in USD.
- **Time:** from the start of your run command to your accepted answer.

A malformed answer is rejected with a `400` and does not end the run. A well-formed answer is final, even if it
is wrong. A run without an answer scores quality 0 and is not eligible.

## Leaderboard

Each scored submission runs once in the sandbox on the public dataset and is scored; one run per submission
keeps your model bill down. A submission appears once its run has finished and review is passed; reviewer flags await organizer approval. Each team is ranked by its best
complete submission. The API reports them in its `median_*` fields; with one run per submission, that is
the run's own value.

1. Eligible before ineligible.
2. Higher quality.
3. Lower cost × time: the submission's cost in USD times its time, with cost counted as at least $0.01 and time
   as at least 1 second. Cost and time weigh the same: halving your model bill counts as much as halving your run
   time, and among free runs the faster one wins.
4. The submission ID, when everything above is equal.

On the leaderboard's plot, each team is a dot, quality against cost or time; the dotted line is the Pareto front,
the teams no other eligible team beats on both.

Test runs on the public scenario `public-1` show the points for each part of the answer, never the expected
values. Scored runs show quality, eligibility, cost and time without a breakdown.

## Final run

When the hackathon ends, the organizers run every team's latest submission that passed review (organizers resolve outstanding reviewer flags before the final run starts; rejected ones don't count) once by default (or best of three if you opt in before the final run starts) on a private dataset that is similar, not the same. It has the same jobs (a tender portal, a supplier portal, an ERP and a webmail inbox), the same bid rules and the same answer format, but different software, with a different look, navigation and site names, other content and data, and the information split differently across them. Your agent has to handle it as it finds it, and each run's brief states the bid rules. It ranks in the same order as above. Build an agent that works the
answer out from the websites, not one tuned to the public scenario.

## Winners

We pick the winners on three things, roughly in this order:

1. Performance on the private dataset (the final run).
2. Performance on the public dataset (the leaderboard).
3. The elegance of the system.

It is the latest submission, not the best one: your best submission holds your place on the leaderboard; your
latest one that passed review goes into the final run. The final run's results stay with the organizers until they
publish them. Then the leaderboard shows the final results, tagged "Private dataset · final", with each team's
public-dataset result and its change in rank beside them, and the public-dataset board stays at `$API/challenges/rfq/leaderboard?dataset=public` (Markdown:
`leaderboard.md?dataset=public`; API: `GET /v1/challenges/rfq/leaderboard?dataset=public`).

## RFQ scoring

The brief states the calculation rules. Each position has four optional fields that earn independent points:
`base_unit_price` (the EUR scale price per piece before discount), `surcharge_per_unit` (material surcharge plus
ordinary and allocated setup cost per piece, before discount), `packaging_per_unit`, and `rebate_total` (the
project-bonus credit for the **entire position**). Leave a field blank if unanswered; enter zero for a known zero.
The same calculation rules and answer format apply to the public and private datasets. You can also explicitly
omit unfinished positions in the form; completed fields retain their credit. You must sign and submit to score.

| Part (a bid) | Points |
| --- | --- |
| Decision | 5 |
| Tender reference | 5 |
| Discount percent | 5 |
| Total net (to the cent, consistent with the positions) | 5 |
| Arithmetic (every position, and a total that is their sum) | 5 |
| Withdrawn Q&A quantity check (when present) | 5 |
| Dated metal-index check (when present) | 5 |
| Positions (split evenly): material 20 %, quantity 15 %, base price 15 %, surcharge 10 %, packaging 10 %, project bonus 10 %, unit price 10 %, lead time 10 % | 75 minus the checks above |


For a no_bid case: decision 75, tender reference 5, no positions 5, a total of 0 15. A wrong decision costs its
points but leaves the other fields their credit.

Money is compared to the cent. Positions are matched by their position number; extra or duplicate positions cost
one position's share each.

## Final private evaluation: one attempt or best of three

The final runs your latest passed submission on the private set. The default is one attempt.
Before the final run starts, opt in to **Best of 3** on your team page or run
`atira final --best-of 3` (`--best-of 1` opts out). `atira status` shows the choice.
When the final run starts, each team's choice is fixed, including teams still in review.
Opted-in teams get up to three attempts on the same private scenario(s). Your **single best complete attempt**
counts: eligible first, then higher quality, then lower cost × time, with the leaderboard's usual floors.
It is not an average, and a failed attempt does not outweigh a better successful one.
Every attempt is kept. Your team sees its own attempts' logs (private values masked) and, once the organizers publish the final, the results.
Calls using your own model keys can cost up to **3×**. The public leaderboard remains one run per submission
on the public scenario. Review, flags and disqualification work as for any submission.

Flagged results are hidden from the public leaderboard by default. **Show flagged** on the page (or `?flagged=1` on
the leaderboard API) lists them beside the approved ones, tagged "flagged: waiting for an organizer" and without a
rank, so showing them does not change other ranks. An organizer pass makes a flagged result count normally.
