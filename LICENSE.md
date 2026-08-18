# License

Case is dual-licensed by directory. Full texts live in [`LICENSES/`](LICENSES/).

| Path | License | SPDX |
|------|---------|------|
| `control-plane/`, `image/` | [GNU AGPL v3](LICENSES/AGPL-3.0.txt) | `AGPL-3.0-only` |
| `mcp/`, `bin/case`, `web/`, `tests/` | [MIT](LICENSES/MIT.txt) | `MIT` |

The AGPL covers the box and the API: the code that holds your credentials is the
code you can read, and a hosted Case clone must share its changes. Client
surfaces (MCP, CLI, Drive UI) are MIT so they can be wired into any agent without
the copyleft reaching your code.

Each source file carries an `SPDX-License-Identifier` header, so the license of
any single file is answerable without consulting this table.

## What the AGPL means here, in practice

- **Running Case, however you like, including commercially.** No obligation. Use
  it on your laptop, on your servers, for your customers' work.
- **Modifying Case for your own use.** No obligation while you keep it to
  yourself.
- **Offering a modified Case to others over a network.** This is the one that
  bites: you must offer those users the source of your modifications, under the
  AGPL. Running an *unmodified* copy as a service carries no such obligation.
- **Writing an agent that drives Case over MCP or REST.** Not a derivative work.
  Your agent is yours. That is also why the client surfaces are MIT.

## Commercial licensing

The AGPL is not the only way to use Case. A commercial license is available for
teams who want to ship a modified Case as part of a service without the source
obligation in section 13 of the AGPL, or whose procurement rules exclude
copyleft entirely.

Same code, different terms. The hosted image is byte-identical to the one built
from this repository; we do not keep a better version back.

Two things a commercial license does **not** and cannot cover:

- **Third-party components.** It grants rights to Case's own code only. The
  desktop image also contains Debian, Xfce, Chromium, FFmpeg and others, each
  under its own license, which nobody can relicense on their behalf. See
  [NOTICE](NOTICE). Those components are aggregated in the image, not linked
  into Case's source.
- **The name.** See below.

To ask about terms, open an issue at
https://github.com/case-computers/case/issues.

GitHub may label this repository Other: the tree is dual-licensed, which the
license picker does not split by directory. The table above is authoritative.

## Trademark

**Case™** and the Case logo are trademarks of Daemon Labs. Neither the AGPL nor
the MIT license grants any right to use them, and this file grants none either.

You may state accurately that your software is built on, derived from, or
compatible with Case. You may not name a fork, product, or service "Case", or
use the logo, in a way that suggests it is this project or is endorsed by it.
Rename your fork.

## Contributing

Contributions are accepted under a [Contributor License Agreement](CLA.md),
signed once by comment on your first pull request. You keep your copyright; the
project gets the right to sublicense. That grant is precisely what makes the
dual licensing above possible, and it cannot be reconstructed later, which is
why it is asked for up front. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Third-party components

Case ships and builds on other people's work. See [NOTICE](NOTICE).
