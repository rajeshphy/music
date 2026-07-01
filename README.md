# Music Radar

Static YouTube discovery page for future chill, future garage, chillstep, ambient electronic, cinematic chill, melodic downtempo, and liquid chill.

The updater searches YouTube with `yt-dlp`, scores results toward emotional instrumental electronic music, and writes `data/tracks.json`. It stores only public metadata and links.

## Local update

```sh
python3 scripts/update_tracks.py
```

## Data controls

Edit `data/searches.json` to tune:

- search phrases
- per-query weights
- positive ranking terms
- negative ranking terms
- max tracks and max results per query

