# deckwatch (public edition)

A small personal watcher for second-hand sale boards. You tell it the specific things you're hunting for (a model of
film camera, a typewriter, a turntable), and it checks Reddit's buy/sell subreddits and Reverb a few times a day. You
hear about each matching listing once.

It is **read-only**. It never posts, comments, votes, sends messages, or logs in as a user. It reads public listings
and nothing else.

## What it does on Reddit

- **Where it reads:** the sale subreddits for each search's category, by default:

  | category | subreddits |
  |---|---|
  | camera | r/photomarket |
  | typewriter | r/typewriter_swap, r/typewriters |
  | audio | r/AVexchange |
  | computer | r/hardwareswap |

  A search can name its own boards with `"subreddits"`.
- **What it keeps:** posts that are **selling** something ([WTS], [FS], [H], "for sale") and whose title matches one of
  your queries. It drops buying requests ([WTB], "looking for"), posts older than 60 days, and anything well over
  your price limit.
- **How often:**
  - **Without credentials:** one request per subreddit per run to the public `new.rss` feed, at least 6 seconds apart.
  - **With credentials:** a "script" app using application-only OAuth (read-only, no user context). It runs one
    subreddit search per query, at most one request a second, far under the 100 requests/minute limit.
  - **Every request** carries a descriptive User-Agent (`reddit_user_agent` in the config).
- **What it stores:** for each match, only the post's id, title, price, link, and when it was first seen, in a local
  SQLite file. That's so you aren't told twice. Nothing is republished, shared, sold, or used to train anything.

## Setup

Python 3.9+, standard library only.

    cp config.example.json config.json      # edit the searches
    python3 watch.py --dry-run              # try it: prints matches, remembers nothing
    python3 watch.py                        # for real: new matches are printed (and sent to the webhook)
    python3 watch.py --list                 # everything found so far

Run it from cron or a systemd timer, a few times a day.

**Optional Reddit credentials:** create a "script" app (Reddit now reviews new API access requests) and set
`REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` in the environment. Without them the public feeds are used.

**Optional notifications:** set `webhook_url` in the config, or `DECKWATCH_WEBHOOK` in the environment. Any endpoint
that accepts a Discord-style `{"content": "..."}` POST works, e.g. a Discord channel webhook.

## A search

    {
      "name": "film camera",                       # shown on each match
      "category": "camera",                        # picks the default subreddits
      "queries": ["Canon AE-1", "Pentax K1000"],   # a title must contain every word of one query
      "max_price": 250,                            # listings well above this are skipped
      "exclude": ["lens only", "manual"],          # titles with any of these are skipped
      "require": [],                               # if set, titles must contain one of these
      "subreddits": [],                            # overrides the category's boards
      "sources": ["reddit", "reverb"]              # Reverb: its public listings API, used items in the US
    }

## License

MIT, see LICENSE.
