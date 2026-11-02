# UK Companies

UK Companies returns one sheet for a company name or number. Each source stays in its own block, and each row carries an official URL. An empty field stays empty and includes the reason. The tools are `uk_check`, `uk_search`, `uk_officers`, `uk_psc`, `uk_filings`, `uk_charity`, `uk_vat`, and `uk_land`. Linux and macOS are the listed platforms.

The sheet reads Companies House (search, profile, officers, persons with significant control, filing history, and disqualified officers), The Gazette insolvency feed, the Charity Commission, and HM Land Registry Price Paid. `uk_vat` explains why a VAT number is not checked. HMRC Check a UK VAT number API 2.0 asks you to register, and that step takes about two weeks, then asks for OAuth 2.0 client credentials. This plugin does not call HMRC and does not apply for access.

## Install and first call

From this directory, with Hermes 0.21.4 or newer:

```
hermes plugins install "file://$PWD" --enable
hermes uk-companies call uk_vat
```

The install command enables the plugin. `hermes plugins enable uk-companies` just after that only reports that it is already enabled.

`uk_vat` makes no network call and needs no key. The JSON says the VAT number was not checked.

Put a free Companies House key in the active profile `.env` as `COMPANIES_HOUSE_API_KEY`. The signup page is `https://developer.company-information.service.gov.uk/get-started`. Put a free Charity Commission key in the same file as `CHARITY_COMMISSION_API_KEY`. The portal is `https://api-portal.charitycommission.gov.uk/`. Install does not ask for either key. The plugin still loads when either key is unset. A missing key is reported in the sheet, and that source is not called.

Then ask for one company:

```
hermes uk-companies call uk_check --json '{"query":"00445790"}'
```

A pasted https address on `find-and-update.company-information.service.gov.uk` or `api.company-information.service.gov.uk`, with a path that starts `/company/` and a company number, is read as that number on this machine. The pasted address is not requested. The plugin refuses an http address, an address with a user name, and any other host, and it sends nothing for those addresses. The error names which refusal applied. A trailing `;` is dropped. A trailing ` # ` note is dropped only when the text before it is a company number. A company number of 6 or 7 digits is sent as 8 digits with leading zeros.

A name that returns several companies stops in the candidates array. One hit whose legal name is not an exact match also stops there. The next lookup is one company number from that array. A zero count leaves the question open. `company_status` is the word the register returned.

An exact disqualification row has match `exact`. That requires the officer title, that officer's birth month and birth year, and this company's registered name on an order whose `disqualified_from` is today or earlier and whose `disqualified_until` is after today. Companies House lists that officer as surname, forenames, and an optional title. The ban index lists forenames then surname. The sheet treats those two forms as the same name. A search-row date of birth may carry a time. The sheet reads the month and the year. `matched_officers` lists each officer who shares that birth month and year, including `ended_on` when the officer has resigned. When that list has several rows, they are one person when every row shares one `officer_id`. Several officer ids leave the match possible. An order whose end date is today or earlier stays possible. A permission to act for this company keeps the row possible. An order that uses LTD where the register uses LIMITED, or the reverse, still names this company when the other words agree. Search rows are left out when the surname and first name do not both match the officer, or when the birth month and year are present on both sides and differ. A row whose surname and birth month and year match the officer stays when the title spelling differs. That row stays possible. The disqualification block note gives the dropped count. The sheet opens with `disqualified_exact_count` and `disqualified_summary`. The summary names that count and says the count is not a clearance.

A Gazette row has match `exact` when the title agrees with the registered name and the notice date is not before the incorporation date. A notice dated before incorporation stays possible, because a later company can reuse the name. A legal name is exact only when the characters agree after case and spacing are folded. A symbol one name has and the other lacks stays possible. A compatibility character stays as written. The VAT block has status `excluded` and no row on every sheet.

## What is sent, and what stays here

A company name or number is sent to `api.company-information.service.gov.uk` with your Companies House key as HTTP Basic, when that key is set. The registered company name is sent to `www.thegazette.co.uk` with no key. A registered name that contains a double quote is not sent. The gazette block stays `not_read`, and `next_step` names that skip. A backslash in that name is written twice inside the phrase marks, and notice titles are compared with the registered name. When the charity key is set, the registered name is sent to `api.charitycommission.gov.uk` in the `Ocp-Apim-Subscription-Key` header. A lookup that has not read the registered name skips both of those calls, and the typed query stays on this machine. The registered-office postcode is sent to `landregistry.data.gov.uk` as a SPARQL literal, with no key. A Companies House name search that returns 0 companies, omits the total, or pairs a total of 0 with listed rows sets `ok` to false on `uk_search` and fills `next_step`. Officer names are sent back to Companies House as the disqualified-officer query, with the forenames before the surname. Officer names, a month and year of birth, nationality, and addresses are included when Companies House returns them.

The watch file under `plugin-data/uk-companies` stays on this machine. It stores company numbers, company status, and one line per active appointment or PSC record. An officer line holds the name, role, appointed date, appointment id, and officer id. A PSC line holds the name, kind, natures of control, and the PSC record id. Rows that share no id are stored as a count, so two active directors with the same name stay as two. It does not store a date of birth or an address. Nothing is posted to another chat. An HTTP proxy in the environment is not used. Each call reads the keys again from the active profile. The installed tree, including .git, is about 680 KB.

The tools start enabled on every surface. An empty gateway allowlist still leaves them enabled, so a person who can message that surface can call them with your keys. Turn one surface off with `hermes tools disable uk_companies --platform telegram`. The CLI call still runs after that.

Companies House allows 600 requests in 5 minutes. This process stops at that cap and waits 0.6 seconds between Companies House calls. One lookup also stops after 60 seconds. Each request waits at most 20 seconds. The Gazette feed is called once per lookup, with 10 seconds between Gazette calls in the same process. Company search reads at most 20 rows. Officer and PSC lists read at most 2 pages of 50 rows. Filing history reads 1 page of 50 rows. Disqualified-officer search checks at most 20 names and reads at most 3 profiles. A profile is requested only when the search row's birth month and year match that officer. When a matching profile is left unread, or an exact title has no birth month and year, `fully_read` is false. Search rows are left out when the surname and first name do not both match the officer, or when the birth month and year are present on both sides and differ. A row whose surname and birth month and year match the officer stays when the title spelling differs. The block note gives that count. The published charity name route has no page parameter. This plugin marks a JSON array from that route fully read. A Charity Commission HTTP 200 for that route remains unseen on this machine, so the field names follow the data-definition document. A name-search HTTP 404 is stored as zero rows and leaves the question open. That 404 was not seen with a key on this machine. A 404 on a charity number stays an error. Price Paid keeps 10 sales. A longer upstream list sets `fully_read` to false and the block note names the unread remainder. When one company is resolved and a block has `fully_read` set to false, `ok` stays true and `next_step` names that source. The `requests` object counts only the HTTPS calls made by that tool call.

Price Paid is the price recorded for that sale at the postcode. The list keeps the 10 newest sales. Older sales are left unread. The block note says the rows do not show that the company owns the property, including when the list holds 1 to 9 sales. It covers England and Wales. Contains HM Land Registry data © Crown copyright and database right 2021. This data is licensed under the Open Government Licence v3.0. Address fields in a row (postcode, paon, saon, street, town) are for personal or non-commercial use, or for displaying residential property price information. Other address uses need permission from Royal Mail.

## Morning watch

This plugin does not set approval-card text. Tool arguments are the company name or number you pass. Those arguments do not include an API key.

```
hermes uk-companies watch add 00445790
hermes uk-companies schedule
hermes uk-companies status
hermes uk-companies unschedule
```

`schedule` writes `scripts/uk-companies-watch.py` and creates one cron job named `uk-companies-watch`. The job is local only. Any other deliver target is refused before a job is created. The schedule is `0 8 * * *`. Hermes reads 08:00 in the profile timezone. That timezone is `HERMES_TIMEZONE` when the variable is set, otherwise `timezone` in the profile `config.yaml`, otherwise the machine clock. The run has no model turn. The first successful read stores a baseline and prints no line. A later morning prints one line for each changed field. A status change prints yesterday and today. An officer change or a PSC change prints each added line and each removed line. An empty script sends nothing. A failed read keeps the previous snapshot and prints that the number was unread. At most 20 company numbers are stored. `unschedule` removes the cron job and `scripts/uk-companies-watch.py`.

The morning line is written under the profile home at `cron/output/<id>/<timestamp>.md`. Local delivery leaves that file there. The job fires while a gateway is running for the profile, or while the cron ticker is serving it. `hermes cron status` prints the next run and warns when no scheduler is serving the profile. An empty morning while that scheduler is serving the profile means nothing changed. The warning means the job did not run.

The job remains after you disable or remove the plugin. Copy the id from `hermes uk-companies status` or from `hermes cron list` before you remove the plugin. Stop the leftover job with that id, in the form `hermes cron remove <id>`. `hermes cron remove` with no id exits 2 and the job stays. `hermes plugins remove uk-companies` deletes the plugin directory. It leaves the morning job, `scripts/uk-companies-watch.py`, and `plugin-data/uk-companies/watch.json`. The watch file holds officer names and persons with significant control. `unschedule` removes the job and the script and leaves the watch file. Delete `plugin-data/uk-companies/watch.json` yourself when those names should go.
