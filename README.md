# Pokémon PH Stock Watch — Toy Kingdom + Toys"R"Us

A cloud-only watcher that posts Pokémon Trading Card Game (TCG) product and stock signals to a Discord channel. It runs with GitHub Actions; **nothing needs to be installed or left running on your PC** after setup.

## What it watches

- **Toy Kingdom PH:** the public Pokémon collection, the first public catalogue JSON page, Shopify-style product search suggestions, and public event/news Atom feeds.
- **Toys"R"Us PH:** the public Pokémon and Trading Card Games collections, the first public catalogue JSON page, Shopify-style product search suggestions, and public event/news Atom feeds.
- **Stealth/listing signals:** newly detected Pokémon TCG product listings, including items that are not yet orderable.
- **Stock signals:** listings that change from sold-out/unavailable to orderable according to the public page.
- **Pre-order signals:** listings containing pre-order/reservation/coming-soon wording.
- **Price signals:** product price decreases.
- **Official announcement signals:** new Pokémon TCG-related entries surfaced by the public retailer blog/event feeds.

Official pages it checks:

- Toy Kingdom Pokémon collection: https://www.toykingdom.com.ph/collections/pokemon
- Toy Kingdom events: https://www.toykingdom.com.ph/blogs/event
- Toys"R"Us Pokémon collection: https://toysrus.com.ph/collections/pokemon
- Toys"R"Us Trading Card Games collection: https://toysrus.com.ph/collections/trading-card-games

### Important limitations

This is a best-effort public-page watcher, not a private inventory API. It cannot see stock that the store has not exposed online, and it does not bypass logins, CAPTCHAs, or private social posts. Social-media-only stealth drops may be missed if a retailer doesn't put them on its public product pages or blog feeds. Shopify-style public JSON/search endpoints can change, become rate-limited, or be blocked; the script falls back to visible collection HTML for some endpoints and reports source health changes to Discord. `available` means the product page says it is orderable; it does not reserve a unit or guarantee checkout success.

The scheduled workflow requests a scan **roughly every five minutes**. GitHub explicitly supports schedules as short as five minutes, but queueing/website delays mean alerts are not guaranteed to be instant. For a free GitHub-hosted runner at this frequency, make the repository **public**; the code contains no credentials, and your Discord webhook belongs only in GitHub Secrets. Public scheduled workflows may be disabled after 60 days of no repository activity, so the workflow updates `.keepalive-date` about every 40 days. GitHub docs: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax and https://docs.github.com/en/billing/concepts/product-billing/github-actions

## Setup (no local coding tools required)

### 1. Create your Discord webhook

1. In Discord, open your server and choose the channel for stock alerts.
2. Open **Edit Channel → Integrations → Webhooks → New Webhook** (wording may vary).
3. Name it `Pokémon Stock Watch`, choose the alert channel, then copy the webhook URL.
4. Keep that URL private. Do not paste it in code, commits, messages, or screenshots.

Discord webhook documentation: https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks

### 2. Create a GitHub repository

1. Sign in to https://github.com/new.
2. Name it `pokemon-ph-stock-watch`.
3. Set visibility to **Public** if you want to use GitHub-hosted Actions on this five-minute schedule without using your private-repository minute allowance. This code does not contain the webhook secret. If you prefer a private repository, note that GitHub Free has a monthly Actions-minute allowance and this frequent schedule may exceed it depending on runtime.
4. Create the repository.
5. Upload the contents of this project, preserving the folder structure. In particular, the workflow must be at `.github/workflows/pokemon-stock-watch.yml`. You can upload the files through GitHub's website; no terminal or Git installation is needed.

### 3. Add the Discord webhook as a GitHub Secret

In your repository, go to **Settings → Secrets and variables → Actions → New repository secret**.

- Name: `DISCORD_WEBHOOK_URL`
- Secret: paste the Discord webhook URL

Save it. **Never store the URL in `watcher.py`, `README.md`, `state.json`, or a normal GitHub variable.**

### 4. Allow the workflow to save its baseline

Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions**, then save. The workflow uses this permission only to save the product/announcement baseline and a small monthly keepalive marker.

### 5. Run the first scan

1. Open the repository's **Actions** tab.
2. Select **Pokémon PH Stock Watch** on the left.
3. Click **Run workflow → Run workflow**.
4. Open the run and check its logs. The first successful run saves a baseline and posts a startup message, rather than pinging every listing that already existed. Listings found during the baseline are not treated as new drops.
5. Leave Actions enabled. Scheduled scans will run automatically from the repository's default branch.

The first scan is important: it establishes what products were already visible. If either retailer is unreachable on the initial run, its baseline is established silently the first time its catalogue endpoints become accessible.

## Alerts you should expect

- `NEW LISTING — ORDERABLE` — a TCG listing is newly visible and the page indicates it can be ordered.
- `NEW LISTING — NOT ORDERABLE YET` — a new TCG listing appeared but is not currently marked orderable. It could be an early listing, but is not proof a preorder is open.
- `RESTOCK / POSSIBLE STEALTH DROP` — an existing page changed from unavailable to orderable.
- `PRE-ORDER LISTING` or `PRE-ORDER DETAILS DETECTED` — the listing carries matching preorder/reservation wording.
- `PRICE DROP` — the displayed product price decreased.
- `OFFICIAL-PAGE ANNOUNCEMENT` — a matching Pokémon TCG article was detected in a public retailer feed.

Alerts include the source URL; open it immediately to verify product options, shipping, store location, and checkout availability.

## How the watcher avoids duplicate notifications

`state.json` records products, observed availability/price, announcements, and which feeds were already initialized. The workflow commits this state only when it changes, plus an occasional keepalive marker. On the first run, each retailer/feed gets its own baseline so one inaccessible website does not create a flood of false “new listing” alerts when it comes back.

## Troubleshooting

- **No Discord message:** check that `DISCORD_WEBHOOK_URL` is saved under repository **Secrets**, not Variables, and re-run the workflow manually.
- **Workflow cannot push state:** confirm **Read and write permissions** are enabled, and that the default branch is not protected against this workflow's commits.
- **A source-health warning appears:** open the store collection link; the store may have changed its public endpoint, rate-limited GitHub, or temporarily gone offline.
- **No social-only stealth-drop alerts:** this version polls public product/search pages and retailer blog/event feeds. It does not scrape private social content or guarantee detection of social posts that aren't exposed by public feeds.
- **Too many unrelated products:** edit `CARD_TERMS` in `watcher.py`; overly strict keywords may miss oddly titled booster/collection listings.

## Privacy and safety

No Discord bot token, user password, store account, or personal data is required. The only secret is the Discord incoming webhook URL in GitHub Secrets. Incoming webhooks can post to one channel but cannot read Discord messages.
