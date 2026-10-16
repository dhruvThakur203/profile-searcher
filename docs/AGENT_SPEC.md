# Agent Specification v0.1

## Objective

For each shareholder input row, research publicly available sources for
potential contact information and identity evidence.

Every accepted result must preserve its source URL and evidence.

## Hard identity rules

1. Normalize case, Unicode form, punctuation, and whitespace.
2. Candidate full name must equal the normalized input full name.
3. Partial names are never STRONG.
4. Address similarity is calculated after normalization.
5. STRONG requires address evidence >= 95%.
6. Country, scrip, joint-holder status, and document type are supporting evidence;
   they cannot override a failed hard name rule.
7. Source URL is mandatory for an accepted result; a STRONG match without a source URL remains unaccepted.

## Initial query strategies

- Q1: full name + full address
- Q2: full name + address 1
- Q3: full name + address 2
- Q4: full name + country
- Q5: full name + scrip/company

Future adaptive strategies can remove postal code/country, use distinctive
address fragments, search relevant public-record document types, and
deduplicate repeated URLs.

## Agent loop

INPUT -> NORMALIZE -> PLAN -> SEARCH -> OBSERVE -> EXTRACT -> VALIDATE

If STRONG: preserve evidence and accept.
If not STRONG: choose the next strategy.
If the search budget is exhausted: UNRESOLVED.

## States

- REJECTED: hard identity mismatch
- CANDIDATE: exact name but insufficient address evidence
- STRONG: exact name + >=95% address evidence
- UNRESOLVED: no acceptable result after allowed strategies

## v0.1 boundaries

- no LLM API
- no CAPTCHA bypass
- no login/paywall bypass
- only publicly available, appropriately sourced information
- no production-scale crawling before accuracy evaluation
