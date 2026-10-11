---
title: Configuration
description: "Every tongs configuration key, its type, its default and what reads it."
lead: tongs reads one TOML file. Every key is optional, and a missing file means every default applies.
---

## Where the file lives

tongs finds its configuration directory with
[platformdirs](https://pypi.org/project/platformdirs/).

| Platform | Path |
|----------|------|
| Linux | `~/.config/tongs/config.toml`, or `$XDG_CONFIG_HOME/tongs/config.toml` |
| macOS | `~/Library/Application Support/tongs/config.toml` |
| Windows | `%LOCALAPPDATA%\tongs\tongs\config.toml` |

The terminal app, the desktop app and the MCP server all read the same file.

## How tongs checks the file

tongs checks the whole file when it loads it.

- **Mistakes stop tongs.** A TOML syntax error, a value of the wrong type, a
  number out of range or a malformed `[hosts.<name>]` table gives one message
  that names the file and the section and key, or the line and column of a
  syntax error. The terminal app prints it and exits with status 2. The
  desktop app shows it in a dialog and does not start. The MCP server returns
  it as the error of each tool call until you fix the file.
- **Unknown names are skipped.** A section or key tongs does not know, such as
  a misspelled key, logs one warning and is ignored, so its default applies.
- **Some values fall back.** An invalid `file_content_ttl` or
  `submitted_retention`, or an unknown `theme`, `syntax_theme`, `diff_style`
  or `color_scheme` name, logs a warning and the default applies, as described
  in their sections.

Quote text values: `scan_root = "~/git"`, not `scan_root = ~/git`. Write
numbers without quotes (`mr_list_ttl = 60`) and booleans as `true` or `false`.

## Example

```toml title="~/.config/tongs/config.toml"
[general]
scan_root = "~/git"
scan_depth = 5

[editor]
command = "code --wait"
external_editor_enabled = true

[ui]
theme = "textual-dark"
syntax_theme = "monokai"
diff_style = "unified"
show_draft_mrs = true
ascii_mode = false
color_scheme = "system"

[cache]
mr_list_ttl = 60
diff_ttl = 300
file_content_ttl = "1d"
max_size_mb = 100

[concurrency]
max_parallel = 8
request_timeout = 30

[drafts]
submitted_retention = "24h"

[hosts.work-gitlab]
hostname = "gitlab.example.com"
forge_type = "gitlab"
```

## `[general]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `scan_root` | string | `"~/git"` | Directory to scan for git repositories. `~` is expanded. |
| `scan_depth` | integer | `5` | How many directory levels to walk below `scan_root`. Zero or more. |

`tongs --scan-root DIR` (or `-d DIR`) overrides `scan_root` for one run.

What the scan finds:

- A directory with a `.git` folder is a repository, and so is a git worktree,
  whose `.git` is a file that points at the main repository.
- Symlinked directories are followed, even when they point outside
  `scan_root`. A link counts as one level of `scan_depth`. Each real directory
  is scanned once, so a link loop ends and a repository reached by two paths
  shows up once.
- Directories whose names start with a dot, such as `.cache` or `.worktrees`,
  are skipped.
- A repository inside another repository's working tree, such as a submodule,
  is skipped.
- The terminal repo list shows each project once, even when several checkouts
  of it are found, such as a main checkout and its worktrees or two clones.
  The row opens a main checkout over a worktree.

## `[editor]`

Both apps read the `[editor]` keys.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `command` | string | `""` | The editor command to start for external editing. When empty, tongs uses `$VISUAL`, then `$EDITOR`. |
| `external_editor_enabled` | boolean | `true` | Whether tongs offers external editing. When `false`, the terminal shows a short notice instead of opening an editor, and the desktop app reports external editor access as disabled. |

### In the terminal

++f2++ in the comment editor and in the job log viewer, and ++f3++ for a
suggestion, start `command`. When it is empty they use `$VISUAL`, then
`$EDITOR`, then the first of `nvim`, `vim`, `vi` or `nano` found on your
`PATH`. The job log viewer also accepts `less`. tongs splits the command like
a shell would, without starting a shell, and appends the path of a private
temporary file. The terminal app pauses while the editor runs, so a terminal
editor such as `vim` works here. External editing is not available on
Windows.

### In the desktop app

The desktop app appends the path of a private log export to the command and
starts it without a shell. Use an editor that opens a window and waits, such as
`code --wait` or `kate --block`. Known terminal-only editors, such as `vim`,
`nano` or `less`, are reported as unsupported, because they cannot attach to
the desktop window. `vim --remote-wait`, which hands the file to a running Vim
server, is accepted. A wrapper script must itself start a graphical editor and
wait for it. The desktop app reports that the editor started separately from
confirmation that it read the file.

While the editor runs, the desktop app keeps the export open so that cleanup
removes only that exact file. After 23 hours it lets go and leaves the export in
place. A later export can reclaim it once 24 hours have passed. This timing
assumes the app resumes normally after the computer sleeps. Closing the
desktop app never closes the editor.

## `[ui]`

`color_scheme` applies to the desktop app. The other `[ui]` keys apply to
the terminal app, and the desktop app does not read them.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `theme` | string | `"textual-dark"` | The colour theme of the terminal app, by Textual theme name, such as `textual-light`, `dracula`, `nord` or `monokai`. |
| `syntax_theme` | string | `"monokai"` | The [Pygments style](https://pygments.org/styles/) that colours code in diffs and in the code shown with review threads, such as `friendly`, `github-dark` or `solarized-light`. |
| `diff_style` | string | `"unified"` | The layout a diff opens in: `"unified"` or `"split"`. |
| `show_draft_mrs` | boolean | `true` | Whether draft MRs appear in the inbox. When `false`, the Reviews, My MRs and All Open lists leave them out. |
| `ascii_mode` | boolean | `false` | Draw every screen of the terminal app with ASCII characters only. Turn it on for terminals and fonts that show Unicode symbols as boxes. |
| `color_scheme` | string | `"system"` | The desktop app's colour scheme: `"system"` follows the operating system, `"light"` and `"dark"` force one. |

- **`theme`.** To see the built-in themes, press ++ctrl+p++ and choose
  **Theme**; the choice there lasts until you quit. An unknown name logs a
  warning, shows a notice and uses `textual-dark`. Before tongs 1.1, `theme`
  had no effect and its default was `"monokai"`. A file that still says
  `theme = "monokai"` now gets Textual's Monokai theme; remove the line to
  keep the previous look.
- **`syntax_theme`.** Only the style's token colours and weights are used, so
  the added and removed line backgrounds stay the same. An unknown name logs a
  warning and uses `monokai`.
- **`diff_style`.** Press ++v++ in the diff viewer to switch layouts for that
  review. When the terminal is too narrow for split view, tongs uses unified
  view until it is wide enough. Any other value logs a warning and uses
  `"unified"`.
- **`show_draft_mrs`.** With the default, draft MRs are listed and the inbox
  marks them `D`.
- **`ascii_mode`.** It applies to every screen, dialog and panel. CI statuses
  read `OK`, `FAIL`, `RUN`, `PEND`, `CANC` and `SKIP`. Borders, rules, the
  file tree and list bullets use characters such as `+`, `-`, `|` and `*`,
  Markdown tables have no lines between cells, and the active tab is bold and
  underlined instead of having a bar under it. Keys read `enter` and `up`
  instead of arrows, and loading reads `Loading...`. A title too long for the
  header is cut off without an ellipsis. Text from the forge, such as titles
  and comments, is shown as written.
- **`color_scheme`.** The desktop app's **System**, **Light** and **Dark**
  toggle overrides this value, and the app remembers the choice in its own
  settings, never in this file. Choosing **System** there clears the choice,
  so this value applies again. See
  [Colour scheme](/desktop/workspace/#colour-scheme). Any other name logs a
  warning and uses `"system"`. It does not change the terminal app; use
  `theme` for that.

## `[cache]`

tongs keeps forge responses in a local SQLite database, so that returning to a
screen does not wait on the network.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mr_list_ttl` | integer | `60` | How long, in seconds, a cached list of MRs stays fresh. Zero or more. |
| `diff_ttl` | integer | `300` | How long, in seconds, a cached diff stays fresh. Zero or more. |
| `file_content_ttl` | string | `"1d"` | How long a file read at a commit, to expand diff context, stays in the cache. |
| `max_size_mb` | integer | `100` | The size limit of the cache database, in megabytes. It must be greater than zero. |

Write `file_content_ttl` as a whole number followed by `h` for hours or `d`
for days, for example `"12h"` or `"7d"`. The unit is lowercase, and the value
must be from `"1h"` to `"365d"`. Any other value logs a warning and the
default of one day applies.

- **What is cached.** MR lists, diffs, and the files tongs reads at a commit
  when you expand the context around a diff. Only file bodies of 1 MiB or
  less are kept in the cache database; larger ones are read again in a new
  session. A file at a commit never changes, so commenting on or approving a
  review leaves these entries in place. Job logs are never cached. Reads that check a review's
  current revision always go to the forge.
- **When it refreshes.** An entry older than its time-to-live is fetched again.
  Approving, merging, commenting or submitting a review clears the affected
  entries, so the next read is fresh.
- **When it is full.** When the stored entries pass `max_size_mb`, tongs
  removes the least recently used quarter.
- **Where it lives.** `cache.db` in the platform cache directory:
  `~/.cache/tongs/` on Linux, `~/Library/Caches/tongs/` on macOS and
  `%LOCALAPPDATA%\tongs\tongs\Cache\` on Windows. The directory is private to
  your user, and the file is created with mode `0600`.
- **Clearing it.** Run **Clear Cache** from the command palette (++ctrl+p++),
  or use **Clear cache** in the desktop app. Clearing removes cached responses
  only. It never touches your review drafts.

## `[concurrency]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_parallel` | integer | `8` | The most requests tongs sends to each forge host at once, counting list, CI, GraphQL and job log requests. Each host has its own limit. The terminal inbox also runs at most this many per-row CI lookups per host at once, except while GitHub combines them into GraphQL queries, when one query's worth (about 50) may run together as one request. It must be greater than zero. |
| `request_timeout` | integer | `30` | The HTTP request timeout, in seconds. It must be greater than zero. |

## `[drafts]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `submitted_retention` | string | `"24h"` | How long a submitted [review draft](/guides/review-drafts/) stays in local storage before tongs deletes it. |

Write the value as a whole number followed by `h` for hours or `d` for days,
for example `"24h"` or `"7d"`. The unit is lowercase, and the value must be
from `"1h"` to `"365d"`. Any other value logs a warning and the default
applies.

- **What it changes.** A submitted draft disappears from the draft list in
  both apps at once. It stays in `drafts.db` for this window, then tongs
  deletes it with the record of its submission. tongs checks when either app
  starts and after each submission.
- **What it never deletes.** Drafts you are still editing, drafts being
  submitted, and drafts whose submission was interrupted or has an unknown
  outcome.

## `[hosts.<name>]`

`github.com` and `gitlab.com` work without configuration. Add a table for each
self-hosted instance, so tongs recognizes its remotes when it scans your
repositories. The table name is your own label.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `hostname` | string | (required) | The instance's hostname, for example `gitlab.example.com`. Lowercase, with no scheme, port or path. |
| `forge_type` | string | (required) | `"gitlab"` or `"github"`. |

A missing key, any other `forge_type`, or a hostname such as
`https://gitlab.example.com` or `gitlab.example.com:8443` stops tongs with a
message naming the table and key.

```toml
[hosts.work-gitlab]
hostname = "gitlab.example.com"
forge_type = "gitlab"

[hosts.work-github]
hostname = "github.corp.com"
forge_type = "github"
```

:::caution[Sign in to each host]
tongs uses the credentials of the forge CLI. Sign in `glab` or `gh` against
each self-hosted hostname:

```bash
glab auth login --hostname gitlab.example.com
gh auth login --hostname github.corp.com
```

A `~/.netrc` entry whose `machine` names the host, or a token in the system
keyring, also works. For the
keyring, install tongs with the `keyring` extra (see
[Install and sign in](/getting-started/#optional-extras)), then store the token
under the service name `tongs`:

```bash
# prompts for the token
pipx run keyring set tongs gitlab.example.com
```

See [Credentials](/reference/security/#credentials) for the order tongs
tries them in.
:::

## `[plugins.<name>]`

Each plugin reads its own table. The keys are defined by the plugin.

```toml
[plugins.fleet]
monitor_interval = 30
```

tongs passes the table to the plugin as a dictionary. One key is reserved:
`enabled = false` stops tongs from loading that plugin at all. `enabled` must
be `true` or `false`; tongs does not check the plugin's other keys. See
[Terminal plugins](/extend/terminal-plugins/) and
[Desktop plugin providers](/extend/desktop-providers/).
