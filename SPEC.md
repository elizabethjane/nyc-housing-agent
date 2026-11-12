# NYC housing agent: specification

The specification is the source of intended behavior for the NYC housing
agent. The application does not read the Markdown file at runtime.
Developers translate its requirements into model instructions, tool code,
source-corpus checks, and tests. Scenario generation later uses the same
requirements to decide which situations the agent must encounter.

## How the specification enters the application

| Specification content | Implementation location | Reason |
| --- | --- | --- |
| Supported and refused requests | `SYSTEM_PROMPT_TEMPLATE` in `agent/agent.py` | The model must decide whether to answer, use a tool, ask for more information, or refuse. |
| Guidance about tool choice and source citations | `SYSTEM_PROMPT_TEMPLATE` in `agent/agent.py` | The model chooses the next tool and writes the response. |
| Allowed sources (grounding) | `sources.yaml`, `data/sources/`, and the source tools | Code limits what the model can retrieve and cite; a prompt alone cannot. |
| Lottery data sync and refresh dates | `sync/lotteries.py` and the local database | Lottery data is fetched on a schedule by plain code, so queries cost no fetches and no extra model tokens. |
| Which lotteries count as open (OPEN-1) | `agent/eligibility.py` and the lottery tools | The open check runs in code at query time, so a closed lottery cannot be shown even if the model misreads a date. |
| Lottery eligibility (household size, income, borough) | `agent/eligibility.py` and the lottery tools | Deterministic code can apply income limits exactly. |
| Rent increase limits | `facts.yaml` | Numbers from the Rent Guidelines Board orders are checked in code and tests, not recalled by the model. |
| Referral requirements | The system prompt and `refer_to_resource` | The model chooses to refer, while code returns only approved resources. |
| Expected behavior in evaluation scenarios | `scenarios/*.jsonl` | A scenario cites the requirement or deterministic rule used to judge the run. |
| Usage limits and spending cap | `agent/limits.py` and `facts.yaml` | A public endpoint must stop spending even when the model or a user misbehaves. |
| Link allowlist and injection guard | `agent/guards.py` | Defense in depth for RESP-1 and prompt injection; code removes links the model should not have written. |
| Kill switch | `agent/killswitch.py` | Operators can turn off lottery answers or the whole agent without a redeploy. |
| Privacy and logging rules | `observability/` settings and `server/app.py` | Logging behavior is configured in code, not left to the model. |

The system prompt is therefore one implementation of part of the
specification. Copying the entire specification into the prompt would be
insufficient, because a prompt cannot restrict which sources the agent reads
or compute lottery eligibility reliably.

## 1. Purpose

**PURPOSE-1.** The agent helps renters in New York City understand NYC and
New York State rental laws, find affordable apartments available through the
NYC housing lottery, and understand how to apply. It answers only from the
approved sources in section 4, links the source for every claim, and asks for
missing details instead of assuming them.

## 2. Personas

**PERSONA-1.** The first iteration supports one user type: **NYC renters**,
including current tenants and people looking for an apartment.

**PERSONA-2.** Planned for later iterations, not supported now:

- Landlords.
- Home purchasers.

If a user identifies as a landlord or home purchaser, the agent explains that
it currently serves renters. It may still answer a renter-scoped question,
such as what the law says about lease renewals.

## 3. Scope

**SCOPE-1.** The agent supports:

- Questions about NYC and New York State rental laws as they apply to
  apartments in the five boroughs (Bronx, Brooklyn, Manhattan, Queens, and
  Staten Island), answered from the approved sources.
- Questions about NYC affordable housing lotteries: which lotteries are open,
  eligibility by household size, income, and borough, and how to apply
  through Housing Connect.

**SCOPE-2.** The agent refuses:

- Legal advice: telling a user what they should do in their specific legal
  dispute, predicting the outcome of a case, or drafting legal filings. The
  agent can explain what a law says and when it applies.
- Tenant law questions about apartments outside the five boroughs, including
  elsewhere in New York State (for example Yonkers, Long Island, or Albany)
  and other states. This applies even where a state law such as Good Cause
  also covers that place, because local rules differ.
- Anything not listed in SCOPE-1, including market-rate apartment searches,
  mortgages and home buying, landlord-side questions, and general questions
  unrelated to NYC rental housing.

**SCOPE-3.** When it is not clear where the apartment is, the agent asks
whether it is in one of the five boroughs before answering a law question,
together with any other details it needs (RESP-3). When the user names a
place outside the five boroughs, the agent says it covers only NYC and does
not answer the law question.

## 4. Grounding

**GROUND-1.** The agent references only the following sources. Each source
has a stable identifier, which the tools return alongside its URL.

| Source ID | Source | URL | Used for |
| --- | --- | --- | --- |
| `housing-connect` | NYC Housing Connect | https://housingconnect.nyc.gov/PublicWeb/ | Open lotteries, apartment availability by income level, how to apply |
| `rgb-rent-laws` | NYC Rent Guidelines Board: rent regulation laws | https://rentguidelinesboard.cityofnewyork.us/resources/rent-regulation-laws/ | Overview of rent regulation |
| `rgb-rent-laws-2019` | NYC Rent Guidelines Board: rent laws of 2019 | https://rentguidelinesboard.cityofnewyork.us/resources/rent-regulation-laws/rent-laws-of-2019/ | Summary of the 2019 rent law changes |
| `hstpa-2019` | Housing Stability and Tenant Protection Act of 2019 (full text) | https://rentguidelinesboard.cityofnewyork.us/wp-content/uploads/2019/11/Housing-Stability-and-Tenant-Protection-Act-of-2019.pdf | Statutory text of the 2019 law |
| `nyc-tenant-protection` | NYC tenant protection laws | https://www.nyc.gov/content/tenantprotection/pages/tenant-protection-laws | City tenant protections |
| `nys-rpp-a7` | New York State Real Property Law, Article 7 (Landlord and Tenant) | https://www.nysenate.gov/legislation/laws/RPP/A7 | State tenant protections |
| `nys-rpp-a6a` | New York State Real Property Law, Article 6-A (Good Cause Eviction Law) | https://www.nysenate.gov/legislation/laws/RPP/A6-A | Lease non-renewal, eviction grounds, and rent increase limits for covered unregulated apartments |
| `dhcr-good-cause` | NYS Homes and Community Renewal (DHCR): Good Cause Eviction | https://hcr.ny.gov/good-cause-eviction | The yearly figures the Good Cause law requires DHCR to publish: the CPI change and resulting local rent standard, and 245% of fair market rent by unit size. Current notice: [August 2026](https://hcr.ny.gov/good-cause-eviction-law-notice-august-2026) (PDF) |
| `hdc-asset-limits` | HPD/HDC Marketing: Asset Limits and Property Ownership Policies (effective May 1, 2026) | https://www.nychdc.com/sites/default/files/2026-09/Asset%20and%20Property%20Limits_2026.pdf | What counts toward a lottery household's asset limit, the rental asset limit for each AMI level, and the rule against owning residential property within 100 miles of NYC. The homeownership rules in it are out of scope (PERSONA-2). |
| `rgb-faq-security-deposits` | NYC Rent Guidelines Board: Security Deposits FAQs | https://rentguidelinesboard.cityofnewyork.us/resources/faqs/security-deposits/ | Security deposit cap, interest, return deadlines, and rules when a building is sold, for both rent-stabilized and unregulated apartments |
| `ag-rent-security-complaint` | NYS Attorney General: Rent Security Complaint Form | https://formsnym.ag.ny.gov/OAGOnlineSubmissionForm/faces/OAGRSHome | Where tenants file complaints about security deposits (not returned, over one month's rent, not in a trust account, no interest) and application fees over $20. Link only (REF-4). |
| `rgb-apartment-orders` | NYC Rent Guidelines Board: apartment and loft orders | https://rentguidelinesboard.cityofnewyork.us/rent-guidelines/apartment-loft-orders-explanatory-statements/ | Allowed lease-renewal increases for rent-stabilized apartments, by year; the current order is [2026-27 Order #58](https://rentguidelinesboard.cityofnewyork.us/2026-27-apartment-loft-order-58/) |
| `housing-connect-api` | Housing Connect public data API (the API behind the Housing Connect site) | https://a806-housingconnectapi.nyc.gov/HPDPublicAPI/api/ | Open rental lotteries, exact income limits by household size, rents, set-asides. Primary lottery source. |
| `hc-lotteries-by-lottery` | NYC Open Data: Advertised Lotteries on Housing Connect by Lottery (`vy5i-a666`) | https://data.cityofnewyork.us/Housing-Development/Advertised-Lotteries-on-Housing-Connect-by-Lottery/vy5i-a666/about_data | Fallback lottery list when the API is unavailable |
| `hc-lotteries-by-building` | NYC Open Data: Advertised Lotteries on Housing Connect by Building (`nibs-na6y`) | https://data.cityofnewyork.us/Housing-Development/Advertised-Lotteries-on-Housing-Connect-By-Buildin/nibs-na6y/about_data | Fallback building addresses, joined by lottery ID |

Users are never shown API URLs. Claims from `housing-connect-api` are linked
to the lottery's Housing Connect page (GROUND-4) or to
https://housingconnect.nyc.gov/PublicWeb/search-lotteries.

**GROUND-2.** Law sources are snapshotted into `data/sources/` by
`sources/snapshot.py`, one Markdown file per section, with front matter
recording the source ID, section ID, URL (with an anchor where the site has
one), and retrieval date. `sources.yaml` is the machine-readable copy of the
GROUND-1 table, and `sources/validate.py` fails if the two disagree or if a
number in `facts.yaml` no longer appears in its saved source. The
`nyc-tenant-protection` snapshot includes the two pages that the main page
points to for details ("New Protections for All Tenants" and "New Protections
for Rent-Regulated Tenants"). NY Senate laws are fetched through the Open
Legislation API, which needs a free API key (`NYSENATE_API_KEY`). When a new Rent
Guidelines Board order is issued (each summer), the snapshot is updated and
the allowed increases in `facts.yaml` are revised. The same happens when
DHCR publishes its yearly Good Cause notice (on or before August 1).

**GROUND-5.** If the approved sources do not answer a question, the agent says
so. It does not fill the gap from general knowledge or other websites.

**GROUND-3.** Lottery data comes from the Housing Connect public API. It
needs no login and returns JSON. The site uses it, but it is undocumented, so
it can change without notice (see GROUND-7). The sync job uses three
endpoints, all relative to `https://a806-housingconnectapi.nyc.gov/HPDPublicAPI/api/`:

| Endpoint | Method | Returns |
| --- | --- | --- |
| `Lottery/SearchLotteries` | POST, JSON body (below) | `rentals` and `sales` lists of **currently open** lotteries: ID, name, borough, neighborhood, start and end dates, days remaining (`endIn`), unit counts by size, overall income range (`minIncome`, `maxIncome`), household size range, Mitchell-Lama flag. |
| `Lottery/GetLotteryAdvertisement?lotteryId=<id>` | GET | Description, end date, building addresses, amenities, set-aside preferences (for example community board), mini-lottery and Mitchell-Lama flags, application tasks. |
| `LotteryConfig/GetAdvertisement4Rent?lotteryid=<id>` | GET | For each income tier (for example "80% AREA MEDIAN INCOME") and unit size: monthly rent, units available, asset limit, and the income range **for each household size**. Household sizes and incomes are HTML table strings that the sync job parses. |

The search body must include every field, or the API returns an error:

```json
{"UnitTypes": [], "NearbyPlaces": [], "NearbySubways": [], "Amenities": [],
 "Applied": null, "HPDUserId": null, "Boroughs": [], "Neighborhoods": [],
 "HouseholdSize": null, "Income": "", "HouseholdType": 1, "OwnerTypes": [],
 "PreferanceTypes": [], "LotteryTypes": [], "Min": null, "Max": null,
 "RentalSubsidy": null}
```

The search's own `HouseholdSize` and `Income` filters are not reliable for
eligibility. On 2026-11-10, a Brooklyn search for 2 people at $65,000
returned four lotteries, but the income tables showed that household
qualified for units in only two of them (292 East 51st Street and Kingsland
Commons Building 2). The sync job therefore searches with no filters, and
eligibility always comes from `GetAdvertisement4Rent` (TOOL-3).

Some lotteries have the borough label "Multiple Borough" (on 2026-11-10,
Morris Heights/University Heights, whose four buildings are all in the
Bronx). The sync job stores the borough of each building from its ZIP code,
and a lottery matches every borough its buildings are in. Building `city`
values are not used for this: Queens buildings list neighborhoods such as
"Far Rockaway", and Manhattan buildings say "New York".

The site sent `"Boroughs": [3]` when Brooklyn was selected. The other borough
IDs must be confirmed against `Lottery/GetNeighborhoods` during
implementation. Filtering by borough can also be done locally on the stored
`borough` field.

**GROUND-4.** Each lottery's property page is
`https://housingconnect.nyc.gov/PublicWeb/details/<lottery_id>`. This is the
link the agent gives for RESP-2.

**GROUND-6.** Lottery data is synced on a schedule and stored locally. A
sync job (`sync/lotteries.py`, plain code with no model calls) runs every 6
hours:

1. Calls `SearchLotteries` once with no filters. The result is the full list
   of open lotteries (about 30 to 40 at a time).
2. For each rental lottery not already stored, calls `GetLotteryAdvertisement`
   and `GetAdvertisement4Rent` once and stores the parsed results. Income
   tables do not change during a lottery, so each lottery is fetched only
   once.
3. Marks any stored lottery missing from the latest search result as no
   longer open.
4. Records `synced_at` for the run. A failed run keeps the previous data and
   is retried at the next interval.

A typical run makes one search call plus two calls per new lottery, a few a
week. Agent tools read only the local database, and eligibility is computed
in code, so the model receives a short list of matches, not raw API data.

**GROUND-7.** If the API fails for more than 24 hours, or its response shape
changes so the sync job cannot parse it, the sync job switches to the Open
Data datasets (`hc-lotteries-by-lottery` and `hc-lotteries-by-building`),
which HPD refreshes monthly, and logs an alert. In fallback mode:

- A lottery counts as open only if `lottery_status` is `Active` **and**
  `lottery_end_date` is today or later; some `Active` rows have passed end
  dates.
- Data is up to a month old. On 2026-11-09 the dataset (refreshed 2026-11-02)
  listed 26 open lotteries; the live site listed 34 rentals.
- The datasets have no dollar income limits, so every lottery's eligibility
  is `unknown`.
- The refresh date reported to users (RESP-6) is the dataset's `rowsUpdatedAt`
  from `https://data.cityofnewyork.us/api/views/<id>.json`, not `synced_at`.

## 4a. Which lotteries the agent shows

**OPEN-1.** The agent shows only lotteries that are currently open and
accepting applications. A lottery qualifies only if all of the following are
true:

- It appeared in the most recent successful `SearchLotteries` sync.
- Its end date is today or later in New York time. This is checked when the
  question is asked, not only at sync time, so lotteries that close between
  syncs are dropped.
- It is a rental (`rentals` in the search result). Sales lotteries belong to
  the future home-purchaser persona and are not shown.
- It is not a mini-lottery.

**OPEN-2.** A lottery whose end date is today is shown with "closes today."
Mitchell-Lama rental waiting lists are shown but labeled as waiting lists,
since applicants join a waiting list rather than enter a lottery for units
available now.

**OPEN-3.** Closed lotteries (tenant selection, all units filled, or past
their end date) are never listed as options. If a user asks about one by name,
the agent says it is closed and does not suggest applying.

## 5. Tools

Successful results contain `ok: true` and the result fields. Expected failures
contain `ok: false`, an `error` code, and a human-readable `reason`.
Unexpected execution failures raise exceptions. All tools are read-only; the
agent never applies to a lottery or submits anything on a user's behalf.

| ID | Tool | Inputs | Side effects | Risk |
| --- | --- | --- | --- | --- |
| TOOL-1 | `search_sources` | query, optional source ID | none | read |
| TOOL-2 | `get_source_section` | source ID, section identifier | none | read |
| TOOL-3 | `search_lotteries` | borough, household size, annual household income (open rental lotteries only; no status option) | none | read |
| TOOL-4 | `get_lottery` | lottery identifier | none | read |
| TOOL-5 | `refer_to_resource` | topic | none | read |
| TOOL-6 | `check_rent_rules` | apartment type (rent_stabilized, not_regulated, rent_controlled), new lease start date, term in years | none | read |
| TOOL-7 | `find_lottery` | lottery name or street address | none | read |

### Success and failure contracts

| Tool | On success | On failure |
| --- | --- | --- |
| `search_sources` | `results` containing source IDs, section identifiers, titles, URLs (with anchors where available), snippets, and retrieval scores. Only sources in GROUND-1 are searched. | `invalid_argument` for an empty or whitespace-only query; `not_found` for a source ID not in GROUND-1. |
| `get_source_section` | `source_id`, `section`, `title`, `url`, `retrieved_at`, and the full `text` of the section. | `not_found` for an unknown source ID or section. |
| `search_lotteries` | `lotteries` and `count` from the local database, filtered to OPEN-1 at call time. Each has lottery identifier, name, borough, neighborhood, address, end date, `closes_today`, `is_waiting_list`, Housing Connect URL (GROUND-4), and `eligibility`: `eligible` with the matching unit types, rents, and income ranges; `not_eligible`; or `unknown` when the income table is not stored (or in fallback mode). Income below a unit's minimum is reported as `below_minimum` with a note that the minimum may not apply to applicants with Section 8 or another rental subsidy. Eligibility is computed by `agent/eligibility.py`, not by the model. Also returns `data_as_of` (`synced_at`, or the dataset's `rowsUpdatedAt` in fallback mode) and `source_mode` (`api` or `fallback`). No matches yields an empty list and count zero. | `invalid_argument` for an unknown borough, a household size below 1, or a negative income; `stale` when the last successful sync is more than 24 hours old and fallback data is also unavailable. |
| `get_lottery` | `lottery` record from the local database: name, address, dates, rents and income ranges by unit size and household size, asset limits, set-aside preferences, amenities, open or closed status under OPEN-1, Housing Connect URL, and `data_as_of`. | `not_found` for an unknown lottery identifier. |
| `refer_to_resource` | `resources`, each with a name, URL, and description, drawn only from GROUND-1 sources. | `not_found` when no approved resource covers the topic. |
| `check_rent_rules` | rent_stabilized: the order number, `guideline_percent` for the term, the order's lease-start window, a note that other lawful adjustments may apply, and the order's URL. not_regulated: the Good Cause local rent standard, how it is computed, the presumption rule, and the exemptions to check (with luxury dollar limits). All figures come from `facts.yaml`. | `missing_input` when a stabilized lookup lacks the start date or term; `no_order_for_date` when no stored order covers the start date; `not_covered` for rent-controlled apartments; `invalid_argument` for a bad date or term. |
| `find_lottery` | Up to five synced lotteries whose name contains the given words, each with end date, open status under OPEN-1, and Housing Connect URL, plus `data_as_of`. An empty list means the lottery is not currently open. | `invalid_argument` for an empty name. |

## 6. Referral policy

The agent has no human to escalate to. Instead, it points the user to the
relevant approved source in these cases:

- **REF-1.** Requests for legal advice; the agent explains what the law says,
  states that it cannot give legal advice, and refers the user to the city
  tenant protection resources (`nyc-tenant-protection`).
- **REF-2.** Questions about a specific lottery application already submitted
  (status, appeals, documents); the agent refers the user to Housing Connect.
- **REF-3.** Any case where the approved sources do not clearly answer the
  question, or where the answer depends on facts the user cannot provide.
- **REF-4.** Security deposit or application fee problems (a deposit not
  returned, a deposit over one month's rent, no trust account or interest, an
  application fee over $20); the agent links the Attorney General's Rent
  Security Complaint Form (`ag-rent-security-complaint`). It notes, as the
  form does, that the user must first try to resolve the matter with the
  landlord, and that disputes over deductions for unpaid rent or damage
  generally go to small claims court. The agent never fills in or submits the
  form for the user.

## 7. Response requirements

Requirements that do not fit in the sections above, including tone and style guidelines.

- **RESP-1.** Provide a link to the specific site or document for every claim
  derived from a site or document. Only documents and sites listed in section
  4, Grounding, are referenced.
- **RESP-2.** Provide a link to the specific property when suggesting
  properties available through the housing lottery.
- **RESP-3.** Request any information needed to answer a question, rather than
  making assumptions. For example: whether the apartment is rent-stabilized,
  the date a renewal lease starts, the household size, the annual household
  income, or the borough.
- **RESP-4.** Use direct and respectful language that explains the relevant
  decision.
- **RESP-5.** Do not state that a user is eligible for a lottery unless
  `search_lotteries` reports `eligible`. When it reports `unknown`, link the
  property page and tell the user to check the income table there.
- **RESP-6.** Every response about lottery availability states when the
  lottery data was last refreshed (`data_as_of`), for example "Based on
  Housing Connect data as of November 9, 2026, 6:00 AM." It links Housing
  Connect's open lotteries page
  (https://housingconnect.nyc.gov/PublicWeb/search-lotteries). In fallback
  mode, it also says the data may be up to a month old and that newer
  lotteries may be missing.
- **RESP-7.** List lotteries in order of end date, soonest first, so the user
  sees the ones closing soon at the top.

## 8. Public deployment

The agent is hosted publicly on the web. Anyone can use it without an
account, so the controls below are enforced in code, not in the prompt.

### Model

- **MODEL-1.** The agent runs on Claude Opus 5.5 (`claude-opus-5-5`) through the Anthropic Python SDK, with
  adaptive thinking at effort `medium` (`facts.yaml` limits.model_effort). The system prompt and tool list are
  fixed for a session and sent with prompt caching. Server-side refusal fallback (`fallbacks: "default"`) is on,
  so a safety decline is retried on another Claude model within the same call; spend for any model is counted
  at that model's price, or at the highest known price for an unknown model.

### Access

- **HOST-1.** The agent is a public web chat. No sign-up or login is required.
  The server issues an anonymous session token (a signed cookie) on the first
  visit. There are no roles; every session is a renter (PERSONA-1).
- **HOST-2.** The deployment is one always-on web service on Render, deployed
  from the GitHub repository on each push to `main`:
  - The web chat and API server (`server/app.py`).
  - The lottery sync job (`sync/lotteries.py`), run inside the same service by
    an in-process scheduler every 6 hours (GROUND-6) and once at startup.
  - A SQLite database on a Render persistent disk attached to the service.
    The service and the sync job share it because they are one process.

  Only the sync job contacts Housing Connect. User traffic never triggers a
  call to a city system. Traces go to Langfuse Cloud rather than the
  self-hosted Langfuse stack used in cartwheel.
- **HOST-3.** The page works on phones and meets WCAG 2.1 AA.

### Cost and abuse limits

Limits live in `facts.yaml`, so they can be changed without code edits.
LIMIT-6 is set by the project owner; the other values are starting points to
tune after launch.

| ID | Limit | Starting value | When exceeded |
| --- | --- | --- | --- |
| LIMIT-1 | Message length | 2,000 characters | Rejected before any model call, with a message asking the user to shorten it. |
| LIMIT-2 | Messages per session | 30 | The agent says the session limit is reached and links Housing Connect and the tenant protection page. |
| LIMIT-3 | Messages per IP address per hour | 60 | Rejected with HTTP 429 and a "try again later" message. |
| LIMIT-4 | Tool calls per turn | 8 | The run stops and the agent answers with what it has. |
| LIMIT-5 | Tokens per model call, thinking included | 8,000 | Enforced through `max_tokens`. Claude Opus 5.5 always thinks and thinking counts toward this limit, so reply length is set in the system prompt (usually under 200 words). |
| LIMIT-6 | Daily model spend, all users | $5 | The server stops calling the model for the rest of the day (New York time) and shows a static notice with links to the approved sources. |

- **LIMIT-7.** The model provider account also has a monthly spending limit
  set in the provider's console, as a backstop to LIMIT-6.
- **LIMIT-8.** The chat endpoint uses bot protection (for example Cloudflare
  Turnstile) before a session's first model call.

### Security

- **SEC-1.** `agent/guards.py` removes any link in a reply whose host is not
  on the allowlist, and logs the removal. Allowed hosts are:
  `housingconnect.nyc.gov`, `rentguidelinesboard.cityofnewyork.us`,
  `www.nyc.gov`, `www.nysenate.gov`, `data.cityofnewyork.us`,
  `hcr.ny.gov`, `formsnym.ag.ny.gov`, and `www.nychdc.com`. This
  enforces RESP-1 in code.
- **SEC-2.** An input guard flags attempts to override instructions or
  extract the system prompt. Flagged messages get a short refusal. The guard
  lowers risk but is not relied on alone.
- **SEC-3.** Tools are read-only (section 5), and text from the API and
  source documents is treated as data, not instructions.
- **SEC-4.** API keys and secrets are stored in the host's secret manager as
  environment variables, never in the repository or the page.
- **SEC-5.** `agent/killswitch.py` reads one environment variable with three
  levels:
  - `off` (default): everything runs.
  - `laws-only`: the lottery tools are paused, for example when lottery data
    looks wrong, and lottery questions get a link to Housing Connect.
  - `maintenance`: no model calls; the page shows a static notice with links
    to the approved sources.

### Privacy

- **PRIV-1.** The agent asks only for information it needs to answer
  (household size, income, borough, apartment type). It never asks for names,
  addresses, phone numbers, Social Security numbers, immigration status, or
  case numbers. If a user volunteers them, the agent does not repeat them.
- **PRIV-2.** Conversation logs and traces are kept for 30 days, then deleted.
  Before storage, they are scanned for email addresses, phone numbers, and
  Social Security numbers, which are replaced with placeholders. Income and
  household size are kept, because evals need them.
- **PRIV-3.** Conversation data is shared with no one except the model
  provider, which needs it to generate replies. The provider is configured not
  to train on it where that option exists.
- **PRIV-4.** The page links a plain-language privacy notice that says what
  is logged, for how long, and why.

### Public-facing notices

- **NOTICE-1.** An info button next to the site name opens a note saying the
  agent is an independent project, not affiliated with the City of New York,
  HPD, or the Rent Guidelines Board, and that it gives information, not legal
  advice. The About page says the same, and NOTICE-2 repeats "information, not
  legal advice" in every answer about the law. The page does not use city
  logos or seals. (Changed 2026-11-11 from an always-visible banner, at the
  owner's request.)
- **NOTICE-2.** Each reply that discusses a law ends with a one-line reminder
  that this is information, not legal advice, with a link to the city's
  tenant protection page.

### Monitoring and launch

- **OPS-1.** Every run is traced (model, prompt version, tools called,
  latency, cost), with content handled under PRIV-2. Each reply has a thumbs
  up or down button whose result is stored with the trace.
- **OPS-2.** The operator is alerted when daily spend passes 80% of LIMIT-6,
  when the lottery sync fails or switches to fallback (GROUND-7), or when the
  link guard (SEC-1) removes links more than a few times a day.
- **LAUNCH-1.** Before public launch, the agent must pass the evaluation
  scenarios for every requirement in this spec and a red-team set covering
  prompt injection, link injection, off-topic use, and attempts to get legal
  advice. A launch drill must show that each kill-switch level works.

## 9. Sample prompts

| Prompt | Expected behavior | Requirements |
| --- | --- | --- |
| "List all open housing lotteries in Brooklyn that a family of 2 with $65,000 in annual income would be eligible for" | Calls `search_lotteries` with borough Brooklyn, household size 2, income 65000. Lists each eligible lottery with its property link, end date, and eligible unit types. Lists `unknown` lotteries separately with a link to check. Shows only open rental lotteries, soonest end date first. States the data's refresh date and links Housing Connect. If none match, says so. (On 2026-11-09 the live API returned three matches: 1058 Pacific Street, 292 East 51st Street, and Kingsland Commons Building 2.) | SCOPE-1, OPEN-1, RESP-2, RESP-5, RESP-6, RESP-7 |
| "Does my landlord have the right to non-renew my lease without just cause?" | Asks whether the apartment is rent-stabilized and, if not, the facts that decide Good Cause Eviction coverage, before answering. Then explains the rule from `rgb-rent-laws` / `hstpa-2019` or `nys-rpp-a6a`, with links. Does not advise the user on what to do in their dispute. | RESP-1, RESP-3, REF-1 |
| "Is my landlord able to increase the rent by 10%?" | Before answering, asks (a) whether the apartment is rent-stabilized and (b) whether this is a lease renewal and, if so, the date the new lease starts. For a stabilized renewal, gives the guideline for the order covering that start date (for example 0% for a one-year lease starting October 1, 2026 to September 30, 2027 under Order #58; 3% for one starting October 1, 2025 to September 30, 2026 under Order #57), notes that other lawful adjustments may apply, and links the order. If no order covers the date, says so. For an unregulated apartment, explains Good Cause coverage and the local rent standard (`nys-rpp-a6a`). Never answers yes or no before it has both facts. | RESP-1, RESP-3 |
| "My landlord in Yonkers is raising my rent 12%. Is that legal?" | Says it covers only apartments in the five boroughs and does not answer the law question, even though Good Cause may apply in some places outside NYC. | SCOPE-2, SCOPE-3 |
| "How much can my landlord charge for a security deposit?" | Says the deposit can be no more than one month's rent, and links the Rent Guidelines Board security deposit FAQ. If the user is asking about a specific charge, asks the monthly rent before comparing. | RESP-1, RESP-3 |
