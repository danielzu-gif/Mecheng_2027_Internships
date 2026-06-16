# Summer 2027 ME Internship Watcher

Checks a few actively-maintained internship tracker repos every 30 minutes
for postings that match BOTH a big-tech/hardware company name and a
mechanical-engineering role, and pushes an instant notification to your
phone the moment a new one shows up. Runs on GitHub Actions, so it works
even when your computer is off.

## Setup (about 10 minutes, no coding required)

1. **Install the notification app.** Get the free "ntfy" app from the App
   Store or Google Play. No account or signup needed.

2. **Pick a topic name.** In the ntfy app, tap "+" / "Subscribe to topic"
   and make up a name only you would guess — something like
   `yourname-me-internships-9f2k`. Write it down, you'll need it in step 5.

3. **Create a GitHub repository.** If you don't already have a GitHub
   account, make one at github.com (free). Then create a new repository —
   "New repository" → name it anything (e.g. `me-internship-alerts`) →
   make it **Public** (so the scheduled job gets unlimited free minutes) →
   Create.

4. **Upload these files.** On your new repo's page, click "Add file" →
   "Upload files", then drag in everything from this folder, keeping the
   `.github/workflows/check.yml` file in that exact folder structure
   (GitHub's upload UI preserves folders if you drag the whole `.github`
   folder in, or you can create the path manually with "Create new file"
   and paste in the contents).

5. **Add your topic as a secret.** In the repo, go to Settings → Secrets
   and variables → Actions → "New repository secret". Name it `NTFY_TOPIC`
   and paste in the topic name you picked in step 2. Save.

6. **Enable Actions.** Go to the "Actions" tab. If prompted, click "I
   understand my workflows, go ahead and enable them."

7. **Test it.** Still in the Actions tab, click on "Check ME Internships"
   in the left sidebar, then "Run workflow" → "Run workflow" to fire a
   manual test. Click into the run after ~30 seconds and check the logs —
   you should see something like `Done. 0 new matching posting(s) this
   run.` (0 is expected right now — see note below). If you see an error
   instead, something's misconfigured; the log will usually say what.

That's it. From here it runs automatically every 30 minutes, forever,
for free.

## Why it might say "0 matches" for a while

As of today, no big tech company (Apple, Google, Amazon, Microsoft, Meta,
Tesla, SpaceX, etc.) has posted Summer 2027 mechanical engineering
internships yet. Most non-software engineering internships at big
companies open between September and January, with a few aerospace/
defense firms starting as early as August. Zero matches right now is
expected and correct — the bot is just waiting.

## Customizing what counts as a match

Open `check_internships.py` and edit two lists near the top:

- `BIG_TECH_KEYWORDS` — add or remove companies (currently includes the
  big 5 plus major aerospace/hardware names like Tesla, SpaceX, Boeing,
  Boston Dynamics, Blue Origin, etc.)
- `ME_KEYWORDS` — add or remove role-title keywords if it's missing roles
  you'd want to see, or catching too many you don't.

## Notes / limitations

- This checks three community-maintained tracker repos rather than
  scraping each company's career site directly. Those communities are
  large and update fast (often within hours of a posting going live), but
  it does mean you're one step removed from the original source — always
  double check by applying directly on the company's career site.
- If a tracker repo gets renamed (these projects sometimes roll over to a
  new repo name each recruiting cycle), the corresponding URL in
  `SOURCES` at the top of `check_internships.py` will need updating.
- GitHub disables scheduled workflows on a repo after 60 days of zero
  repo activity. If you go quiet for two months, just push any small
  commit (or re-enable it in the Actions tab) to wake it back up.
