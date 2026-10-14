# Credits and source attribution

Rockbox Library Manager was created as a desktop helper for
[PodBox](https://github.com/anthonyfletcher/podbox).

## Anthony Fletcher and PodBox

Full credit for PodBox and the original **Rockbox Music Artwork Fetcher** goes to
[Anthony Fletcher (@anthonyfletcher)](https://github.com/anthonyfletcher).
His artwork tool provided the foundation on which this application was built.

- Original project: [anthonyfletcher/podbox](https://github.com/anthonyfletcher/podbox)
- Original tool and documentation: [tools/art_fetch](https://github.com/anthonyfletcher/podbox/tree/master/tools/art_fetch)
- Original source: [tools/art_fetch/art_fetch.py](https://github.com/anthonyfletcher/podbox/blob/master/tools/art_fetch/art_fetch.py)
- Adapted component here: [rockbox_manager/artwork_engine.py](rockbox_manager/artwork_engine.py)

The artwork engine retains and adapts the original tool's artist and album
artwork lookup, candidate scoring, image processing, and caching workflows.
Changes made here include integration into a Python package, desktop interface
support, shared application versioning, and artwork workflow adjustments.

The desktop interface and additional library-management features are maintained
in this companion project by [cyberdeliaAI](https://github.com/cyberdeliaAI).
Full credit for the original artwork tool remains with Anthony Fletcher.

## Tagcast

The online sources in [rockbox_manager/sources.py](rockbox_manager/sources.py)
(MusicBrainz release groups, Deezer, Apple Music, Discogs, Last.fm, TheAudioDB and
fanart.tv lookups, title matching and per-host request pacing) are adapted from
[Tagcast](https://github.com/cyberdeliaAI/tagcast)'s `sources.py`, a metadata
editor for iBroadcast by the same author, published under the MIT licence. They
are included here under GNU GPL v2 like the rest of this application.

The stable-release checker and its regression tests are adapted from Tagcast
0.41.1. Album-detail and track-health behavior also takes inspiration from its
read-only summaries. The checker retains the upstream MIT notice in
`rockbox_manager/updates.py`; the MIT notice is reproduced below for source
and packaged documentation.

```text
MIT License

Copyright (c) 2026 Cyberdelia

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Rockbox and other contributors

PodBox builds on [Rockbox](https://www.rockbox.org/) and other projects credited
in [PodBox's development credits](https://github.com/anthonyfletcher/podbox#development-credits).
Thank you to those authors and contributors for the underlying player software
and ecosystem.

Artwork displayed or downloaded by the application belongs to its respective
creators and rights holders. Python dependencies retain their own licenses.

## License

PodBox declares its source and additions licensed under the GNU General Public
License, version 2. Rockbox Library Manager, including its adapted artwork engine,
is distributed under GNU GPL v2. The full text is included in [LICENSE](LICENSE),
copied verbatim from [PodBox's docs/COPYING](https://github.com/anthonyfletcher/podbox/blob/master/docs/COPYING).

These attribution and license notices were added on 2026-09-25 to correct their
omission from the initial publication.
