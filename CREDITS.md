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
