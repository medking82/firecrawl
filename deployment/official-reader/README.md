# GA401 Official Reader

`official-read` reads official regulator pages that block Firecrawl, Magpie
`web_fetch` and headless Playwright (NMPA answers them with a 412 JavaScript
challenge, then 400). It reuses Forma's reviewed `readOfficial` from
`report-browser.mjs`, which drives the headed `forma-cde-reader` Chrome on GA401
(xvfb, China egress, CDP `127.0.0.1:9333`). Only the `forma` account can reach
that port, so the wrapper runs the reader with `sudo -n -u forma`.

Supported hosts are the reader's allowlist: `nmpa.gov.cn`, `cde.org.cn`,
`chinadrugtrials.org.cn`, `ema.europa.eu`, `pmda.go.jp`, `fda.gov` and the
disclosure wires. Other URLs fail with `unsupported_host` before Chrome is used.
Use Magpie `web_search`/`web_fetch` or Firecrawl for everything else.

## Use

```bash
official-read https://www.nmpa.gov.cn/ylqx/ylqxggtg/index.html          # Markdown, links listed
official-read <url> --offset 20000 --max-chars 20000                      # next part
official-read <url> --json                                                # reader JSON without page HTML
ssh ga401 official-read <url>                                             # from the Ubuntu PC
```

Calls are serialized with a lock in `$XDG_STATE_HOME/official-reader.lock`
(waits up to 150 s, then `reader_busy`); each read has a 120 s limit. Errors
print `official-read failed: <code>` and exit 2, for example
`official_browser_homepage` (the URL no longer exists and NMPA served its
homepage) or `official_browser_page_redirect`. The shared Chrome also serves
Forma report evidence, so keep reads to what a task needs.

## Install On GA401

Requires `/etc/sudoers.d/90-marck-forma` (`marck ALL=(forma) NOPASSWD: ALL`) and
Forma's installed reader at `/home/marck/services/forma-v2-report-evidence/`.

```bash
sha=$(git rev-parse --short HEAD)
dest=/home/marck/services/official-reader/releases/$sha
ssh ga401 "mkdir -p $dest ~/.local/bin"
scp deployment/official-reader/official_read.py ga401:$dest/
ssh ga401 "chmod 755 $dest/official_read.py && ln -sfn $dest /home/marck/services/official-reader/current \
  && ln -sfn /home/marck/services/official-reader/current/official_read.py ~/.local/bin/official-read"
```

Roll back by pointing `current` at an earlier release directory.

## Test

```bash
python3 -B -m unittest discover -s deployment/official-reader -p 'test_official_read.py'
```
