# Social Media Toolkit

Browser-based crawlers for Weibo, Rednote/Xiaohongshu, and Douyin. Data is stored in local DuckDB files with one database per platform by default.

## Features

- Weibo author crawling, author profile lookup, comments, and keyword search.
- Weibo keyword advanced filters for post type, content type, and custom time ranges.
- Rednote author crawling and keyword search backed by the `search/notes` network API.
- Douyin author profile/video crawling and comment collection.
- Task IDs saved on crawled records for later retrieval.
- Textual TUI via `main.py ui`.
- Flask dashboard for browsing Weibo, Rednote, and Douyin DuckDB data.

## Requirements

- Python `>=3.14`
- `uv` for dependency management
- A browser session that can log in to the target platforms

Install dependencies:

```bash
uv sync
```

Run commands with the project virtual environment:

```bash
uv run main.py --help
```

## Storage

DuckDB is the only storage backend. By default each platform writes to:

- `data/weibo.duckdb`
- `data/rednote.duckdb`
- `data/douyin.duckdb`

Override the database path with the root `--db` option:

```bash
uv run main.py --db data/custom.duckdb weibo keyword 卫健委
```

Set the root media download directory with `--media-dir`. The crawler creates a platform subdirectory inside it, such as `downloads/media/weibo`, `downloads/media/rednote`, or `downloads/media/douyin`:

```bash
uv run main.py --media-dir downloads/media rednote keyword 卫健委
```

Media download is enabled by default. Disable it with `--no-download-media`:

```bash
uv run main.py --no-download-media rednote keyword 卫健委
```

Known collections are routed to physical DuckDB tables:

- `weibo_authors`, `weibo_posts`, `weibo_comments`
- `rednote_authors`, `rednote_posts`, `rednote_post_metadata`, `rednote_post_media_files`, `rednote_comments`
- `douyin_authors`, `douyin_posts`, `douyin_comments`
- `tasks`

Each platform table keeps the full JSON payload in a `data` column and exposes indexed columns such as `task_id`, `author_id`, `post_id`, `keyword`, `status`, and `url`.

## Browser Profiles And Login

The crawlers use `cloakbrowser` and persistent browser profiles by default:

- `data/weibo-browser-profile`
- `data/rednote-browser-profile`
- `data/douyin-browser-profile`

On first run, keep the browser visible, log in manually, then continue the task when prompted. Avoid `--headless` until login state has been saved.

Global options must be placed before the command:

```bash
uv run main.py --headless weibo keyword 卫健委
```

## Task IDs

Every crawl records a task ID. You can pass one explicitly:

```bash
uv run main.py --task-id weibo-keyword-health-202606151000 weibo keyword 卫健委
```

If omitted, a task ID is generated as:

```text
<platform>-<scrape_type>-<condition>-<YYYYMMDDHHMM>
```

Example:

```text
weibo-keyword-卫健委-202606151000
```

## CLI Usage

### Weibo

Crawl posts by author:

```bash
uv run main.py weibo author 1234567890
```

Use local post IDs already saved in DuckDB:

```bash
uv run main.py weibo author 1234567890 --from-local
```

Fetch author profile info:

```bash
uv run main.py weibo author-info 1234567890 9876543210
```

Keyword search:

```bash
uv run main.py weibo keyword 卫健委 --max-pages 3
```

Keyword search with advanced filters:

```bash
uv run main.py weibo keyword 卫健委 \
  --post-type hot \
  --post-type original \
  --content-filter picture \
  --time-from 2026-05-01-13 \
  --time-to 2026-06-01-2
```

Supported `--post-type` values:

- `all`
- `hot`
- `original`
- `following`
- `verified`
- `media`
- `viewpoint`

If `all` is selected, other post type filters are ignored.

Supported `--content-filter` values:

- `all`
- `picture`
- `video`
- `music`
- `link`

If `all` is selected, other content filters are ignored.

`--time-from` and `--time-to` accept:

- `YYYY-MM-DD`
- `YYYY-MM-DD-HH`

When `--time-to` is provided, it must be later than `--time-from`.

### Rednote / Xiaohongshu

Crawl posts by author:

```bash
uv run main.py rednote author AUTHOR_ID
```

Keyword search:

```bash
uv run main.py rednote keyword 西浦
```

Rednote keyword search opens:

```text
https://www.xiaohongshu.com/search_result_ai?keyword=<keyword>&source=web_explore_feed
```

It listens for POST responses to:

```text
https://so.xiaohongshu.com/api/sns/web/v2/search/notes
```

Each note item is normalized into `rednote_post_metadata`. The crawler stops when either a visible `.end-container` appears or no new matching request is seen for 60 seconds.

Process pending local Rednote posts:

```bash
uv run main.py rednote keyword 西浦 --from-local
```

### Douyin

Crawl videos by author:

```bash
uv run main.py douyin author SEC_USER_ID
```

Skip comments:

```bash
uv run main.py douyin author SEC_USER_ID --no-comments
```

Fetch author profile info:

```bash
uv run main.py douyin author-info SEC_USER_ID
```

## TUI

Open the Textual interface:

```bash
uv run main.py ui
```

The TUI builds the same CLI commands, previews them, runs them, and streams output. For login prompts, complete login in the browser and press **Continue** in the TUI.

Root options still go before `ui`:

```bash
uv run main.py --media-dir downloads/media --no-download-media --headless ui
```

## Dashboard

Run the Flask dashboard for all platform databases:

```bash
uv run main.py dashboard
```

Then open:

```text
http://127.0.0.1:5000
```

Use `--host`, `--port`, and `--debug` as needed.

By default the dashboard reads `data/weibo.duckdb`, `data/rednote.duckdb`, and `data/douyin.duckdb`. Use the root `--db` option before `dashboard` to point the dashboard at one DuckDB file that contains platform tables.

## Inspecting DuckDB

Open a database in Python:

```python
from storage import DuckDBDatabase

db = DuckDBDatabase("data/weibo.duckdb")
posts = db.list("weibo_posts_raw")
db.close()
```

Or query with DuckDB SQL:

```python
import duckdb

con = duckdb.connect("data/weibo.duckdb")
rows = con.execute("""
    select task_id, keyword, count(*)
    from weibo_posts
    group by task_id, keyword
    order by count(*) desc
""").fetchall()
con.close()
```

## Development Checks

Compile the project:

```bash
uv run python -m compileall main.py crawler storage dashboard ui.py
```

Check CLI wiring:

```bash
uv run main.py --help
uv run main.py weibo --help
uv run main.py rednote --help
uv run main.py douyin --help
```
