# GitHub Action: CI-failure risk (M5)

The composite action in [`integrations/github-action/`](../../integrations/github-action/)
computes the change facts of [`docs/data/FEATURE_DEFINITIONS.md`](../data/FEATURE_DEFINITIONS.md)
from git **inside the customer's runner**. It calls
[`POST /api/v1/risk/assessments`](../api/RISK_ASSESSMENTS.md) and reports the score in the job
summary and in the step outputs.

> This is a CI-failure risk estimate from historical patterns, not a production outage
> probability.

## 1. Setup

1. **Create an API key with scope `risk:write` and nothing else** (ADR-0016). Until the UI ships
   (M10), use the bootstrap CLI against the tenant's database. It prints the key once, on stdout:

   ```bash
   uv run python -m rl_api.admin create-key --org acme --scopes risk:write --name github-actions
   ```

2. **Store it as a repository or organization secret**, for example with
   `gh secret set RELEASELENS_API_KEY` (paste the key when prompted). Never commit it and never put
   it inline in a workflow. A key that leaks can only create risk assessments for that tenant.
   Revoke it and create a new one.
3. **Add the workflow.** Copy
   [`integrations/github-action/examples/releaselens.yml`](../../integrations/github-action/examples/releaselens.yml)
   to `.github/workflows/`. Keep `fetch-depth: 0` on `actions/checkout`. Other repositories pin the
   action by full commit SHA:
   `uses: ayushman46/releaselens/integrations/github-action@<sha>`. This repository dogfoods it
   with `uses: ./integrations/github-action`.
4. **Optional:** set the repository variable `RELEASELENS_API_URL`. When it is unset, the action
   uses its default `https://api.releaselens.dev`, a placeholder for the hosted service. A local
   stack listens on `http://localhost:28000`, which only a self-hosted runner on the same machine
   can reach.

## 2. Inputs and outputs

The full tables are in the [action README](../../integrations/github-action/README.md#inputs).

- **Inputs:** `api-key` (required, from a secret), `api-url`, `fail-on-band`
  (`none`/`HIGH`/`CRITICAL`), `language` (`java`), `src-globs`, `test-globs`, `timeout-seconds`,
  `previous-sha` and `working-directory`.
- **Outputs:** `risk-score`, `risk-band`, `assessment-id`, `details-url` and `status`
  (`ok`/`unavailable`/`error`/`skipped`).

| Situation | Without `fail-on-band` | With `fail-on-band: HIGH` |
|---|---|---|
| Score returned, band below the gate | pass | pass |
| Band at or above the gate | pass | **fail** |
| API has no model (HTTP 503) | notice, pass | **fail** (fail closed) |
| Network error, 401/403/422, 5xx after retries | warning, pass | **fail** |
| Empty key (e.g. a fork PR without secrets) | notice, pass | **fail** |

Network errors, HTTP 429 and 5xx are retried with exponential backoff (2 s, 4 s, 8 s; at most 4
attempts) within `timeout-seconds`. A 503 that carries the API's JSON error envelope means "no
model loaded" and is not retried. The API is idempotent per
`(repository, commit_sha, event, pull_request)` for 10 minutes, so retries never create duplicates.

## 3. Privacy: what leaves the runner

Only this JSON body is sent:

| Field | Content |
|---|---|
| `repository`, `branch`, `event`, `pull_request` | identifiers from the GitHub event |
| `commit_sha`, `previous_built_sha` | commit SHAs |
| `triggered_at`, `ci_run_url` | time of the run and its URL |
| `extractor` | `{"name": "releaselens-action", "version": "0.1.0"}` |
| `change_facts` | the 20 aggregate numbers and booleans of §4 |

**Never sent:** source code, file contents, diffs, file paths or names, commit messages, author
names or emails. Author emails are compared only in memory inside the runner, to count the team
and decide `by_core_team_member`. The extractor's `--diagnostics-out` file (null reasons) stays
on the runner.

The key must be a secret with scope `risk:write`. The action masks it (`::add-mask::`), writes it
to a `0600` header file that `curl` reads, so it never appears in a process list or command line,
and deletes the file on exit. It sends the key only to `api-url` and does not follow redirects. A
plain `http://` URL to a non-local host triggers a warning.

## 4. How the facts are computed

The extractor is [`extract_change_facts.py`](../../integrations/github-action/extract_change_facts.py):
Python 3.9+, standard library only, and the `git` CLI. It is deterministic: the same repository
state and arguments always give byte-identical output. A fact that cannot be proven is `null`,
never `0`.

**Built commits (§1).** For a **push**, the previous built commit is the event's `before` SHA. For
a **pull request**, the trigger commit is the **PR head** (not the test merge commit) and the
previous built commit is the **PR base**, so the built commits are the PR's own commits back to the
first merge. The walk is `git rev-list --first-parent <head> ^<previous>`. It stops at the previous
build (excluded), the first merge commit (included) or the root. Stopping at the first merge makes
this identical to the topological walk. There is no previous build when `before` is all zeros
(a new branch), on events other than `push`/`pull_request`, or on a first run. Then only the head
commit is used.

| Fact | Implementation |
|---|---|
| `src_churn` / `test_churn` | `git diff-tree --numstat -M` per built commit (a merge against its first parent, a root against the empty tree), summed. Binary files count 0. A pure rename counts 0, and a rename with edits counts the edited lines. |
| `files_added` / `files_deleted` / `files_modified` | Unique paths with raw status `A` / `D` / `M` across the built commits. A path can be in two sets (added in one commit, modified in a later one). Renames (`R`), copies and type changes count in none. |
| `src_files` / `doc_files` / `other_files` | Unique touched paths (rename destinations included). Programming files (source + test), markup/prose, and everything else, from a pinned extension table derived from Linguist. |
| `tests_added` / `tests_deleted` | Net diff from the parent of the oldest built commit (the previous build on a linear walk) to head, restricted to the language's test files. `+` / `−` lines matching `@Test` or `void test…(`. Comment lines are skipped and string literals blanked. |
| `sloc` | Non-blank, comment-stripped lines of the language's **non-test** files tracked at head. `null` (with a warning) when there are none: such a repository is outside the model's training domain, and 0 is a value no training build has. |
| `test_lines_per_kloc` / `test_cases_per_kloc` / `asserts_per_kloc` | Test-file code lines / test-case lines / assert lines, per 1,000 `sloc`, rounded to 4 decimals. `null` when `sloc` is unknown. |
| `num_all_built_commits` | Number of built commits. |
| `commits_on_files_touched` | Distinct non-merge commits reachable from head (`--full-history`) with committer date in [start − 90 d, start] that touched any path of the build (both sides of renames), minus the built commits. |
| `team_size` | Distinct lower-cased, mailmap-normalised author emails (`%aE`) of commits reachable from head in the same window, merges included (mergers count), minus the built commits. |
| `by_core_team_member` | Every author of the built commits is in that team. |
| `repo_age_days` | `floor((start − earliest root commit's committer time) / 1 day)`. |
| `is_pr` | `event == pull_request`. |

`num_commits_in_push` (not used by the model) and `description_complexity` (excluded as uncertain)
are not sent. Missing keys mean "unknown" to the API.

**Java counter.** The lexer handles `//` and `/* */` comments, Javadoc, strings with escapes
(`"//"` and `"/*"` inside a string are not comments), char literals such as `'"'`, and text blocks
(`"""`). A blob with a NUL byte in its first 8,000 bytes is binary and counts 0. The test-case
pattern counts **lines**, like TravisTorrent's line filter, so `@Test public void testX()` on one
line counts once, and on two lines counts twice. Assert lines are `assert…(` calls
(`assertEquals(`, `assertThat(`, `Assertions.assertTrue(`) and Java `assert` statements. Import
lines never count. Counts are cached per blob id, so identical files are counted once.
`--cache-file` persists the cache between runs: pair it with your own pinned `actions/cache` step
if you want it.

**Adding a language.** One `LanguageSpec` entry in `LANGUAGES` holds the extensions, test
directory names, test filename globs, the comment/string lexer, and the test-case and assert
patterns.

## 5. History depth

| Clone | What is sent |
|---|---|
| `fetch-depth: 0` (recommended) | every fact |
| shallow, but deep enough to reach the previous build | change-size facts. Team and hotness facts only if every shallow-boundary commit predates the 90-day window. `repo_age_days` is `null`. |
| `fetch-depth: 1` (actions/checkout default) | `sloc` and test density. With no previous build, `num_all_built_commits = 1`. Everything else is `null`. |
| previous build not in the clone (shallow, or force-pushed away) | the built commits are unknown: change-size facts, `num_all_built_commits`, team and hotness are `null` |

For a pull request whose head commit was not fetched, the action falls back to the checked-out test
merge commit. Its first-parent diff is the PR's change. It still reports the PR head SHA as
`commit_sha`.

## 6. Work caps

Exceeding a cap makes the affected facts `null`, never a partial count. The caps are checked on the
tree before any work, so results never depend on the cache.

| Cap | Default | Facts set to `null` |
|---|---|---|
| Built commits | 1,000 | change-size facts and hotness (`num_all_built_commits` stays exact) |
| Language bytes at head | 256 MiB | `sloc` and the three density ratios |
| Language files at head | 100,000 | same |

As a measured reference, 8,000 Java files (104 MiB) take 7.3 s cold and 2.7 s with a warm cache
(Apple M-series, Python 3.12).

## 7. Deviations and resolved ambiguities

These are decisions where FEATURE_DEFINITIONS left room. The M5 feature-fidelity check measures
their effect on cloned repositories.

1. **Source files** are files of *any* programming language in the pinned table, not only
   `.java`, as the contract says. `sloc` and the test-case counts use only the configured
   language.
2. **Test directories** match whole path components (`test`, `tests`, which covers `src/test/`),
   not substrings. So `latest/Foo.java` and `contest/Foo.java` are source, not tests.
3. **`@Test\b`** does not match `@TestFactory` or `@TestInstance`. The `test…(` rule needs a
   `void` signature.
4. **Sign of tests ±:** `+` lines are "added". The possible swap noted in the audit is not
   replicated.
5. **Team excludes the built commits.** Otherwise `by_core_team_member` would always be true for
   pushes. `team_size` is `null` when the built commits are unknown.
6. **90-day windows use committer dates.**
7. The **trigger commit is always built**: a re-run, or a push whose `before` is head or a
   descendant of it, gives `[head]`, not 0 commits.
8. **Default `triggered_at`** of the CLI is the head's committer date, which keeps it
   deterministic. The action passes the actual run time.
9. **Rename detection** is pinned (`-M`, rename limit 1,000, Myers diff), so a runner's git
   config cannot change the numbers.

## 8. Troubleshooting

- *"Shallow clone detected"*: set `fetch-depth: 0` on `actions/checkout`.
- *`status=skipped`*: the key is empty. Pull requests from forks get no secrets.
- *HTTP 401/403*: the key is wrong, revoked or expired, or lacks scope `risk:write`.
- *HTTP 422*: the request did not validate. Check the extractor version against the API contract.
- To see why a fact is `null`, run the extractor locally with `--diagnostics-out` (see the README).
