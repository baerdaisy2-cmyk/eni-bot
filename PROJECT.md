# PROJECT.md — ENI Monitor

Canonical semantics. Read this before touching anything.
`semantics.py` parses this file into SQLite and reports where the two disagree.

Rules for editing:
- Facts live in the tables below. Prose outside them is not parsed.
- Never mark a claim verified without a command that proves it.
- If a line here and the code disagree, the code is right and this file is stale. Fix this file.

---

## IDENTITY

| key | value |
|---|---|
| name | ENI Monitor |
| root | ~/eni-bot |
| purpose | track whether my own Reddit comments survive, and learn which subs remove them |
| handle | Routine_Seesaw1095 |
| marker | prv8sup |
| stack | python3.11+, flask, requests, sqlite3 (stdlib), hand-rolled css |
| posts_on_my_behalf | no |

---

## DECISIONS

Settled. Do not reopen without saying why.

| id | decision | rationale |
|---|---|---|
| d1 | UpvoteHub is out of scope | later-for-a-friend thing, not this build |
| d2 | one account, own identity, single handle | no multi-account operation |
| d3 | no mirror rotation on 429/403 | retrying a rate limit elsewhere defeats it by design |
| d4 | Reddit data export declined | own-activity only, snapshot not live |
| d5 | live data source handled by me, separately | backend must stay pluggable |
| d6 | OAuth primary, Redlib read-only fallback | one stable source beats eight flaky mirrors |
| d7 | comment generator drafts only, never auto-posts | I read and post everything myself |
| d8 | zero runtime AI cost in core logic | scoring, diff, blacklist are pure python math |

---

## FILES

`verify` runs and must exit 0. `state` is what I believe; semantics.py checks it.

| path | role | state | verify |
|---|---|---|---|
| config.py | settings, markers, thresholds, .env loader | ok | python3 -c "import config" |
| db.py | sqlite spine, 4 tables | ok | python3 -c "import db; db.init()" |
| score.py | ranking, pure math | ok | python3 -c "import score" |
| reddit.py | pluggable fetcher, oauth + redlib | ok | python3 -c "import reddit" |
| monitor.py | snapshot diff, blacklist | ok | grep -q pending_logs monitor.py |
| import_export.py | zip import + deletion diff | ok | grep -q pending_logs import_export.py |
| comment_gen.py | llm draft scaffold | scaffold | python3 -c "import comment_gen" |
| app.py | flask routes | ok | python3 -c "import app" |
| templates/index.html | dashboard | ok | test -f templates/index.html |
| templates/logs.html | log table | ok | test -f templates/logs.html |
| templates/post.html | single post view | ok | test -f templates/post.html |
| static/style.css | shared styles | ok | test -s static/style.css |
| test_fixture.py | proves import + diff + blacklist | ok | test -f test_fixture.py |
| dropbox.py | ngrok file writer | untested | python3 -c "import ast,sys; ast.parse(open('dropbox.py').read())" |

---

## ASSUMPTIONS

Unverified means unverified. Do not build on a row marked `no`.

| id | assumption | verified | how to settle it |
|---|---|---|---|
| a1 | mod-removed comments still appear in my own data | no | compare two snapshots after a known removal |
| a2 | rank_score is meaningful in snapshot mode | yes | python3 score.py - ranks on own-comment density and survival |
| a3 | fast deletion implies a mod or filter | no | heuristic only, never claimed as certainty |
| a4 | the diff engine catches gone/back/still-gone | yes | python3 test_fixture.py |
| a5 | style.css is served by flask | yes | curl -sI localhost:5001/static/style.css |
| a6 | sqlite write lock is resolved | yes | python3 test_fixture.py |

---

## OPEN

| id | item | blocked on |
|---|---|---|
| o1 | live data source | me |
| o2 | comment formula | me |
| o4 | ngrok + dropbox live test | me |

---

## NEXT

| step | action |
|---|---|
| 1 | plug in the live data source |
| 2 | hand over the comment formula |
| 3 | ngrok + dropbox live test |
