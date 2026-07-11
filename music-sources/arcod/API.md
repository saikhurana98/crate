# arcod.xyz API (reverse-engineered from arcod.xyz.har)

A **Qobuz proxy**. No auth/credentials — requests carry only `Referer: https://arcod.xyz/`
and a browser `User-Agent`. Guest rate-limited (`/api/v2/guest/rate-limit`). Base
`https://arcod.xyz`; file host `https://dl.arcod.xyz`.

## 1. Search — `GET /api/get-music?q=<query>&offset=0`
`q` = free text OR a Spotify track URL. Returns:
```
{"success":true,"data":{"query":..,
  "albums":{"total":N,"items":[...]},
  "tracks":{"total":M,"items":[ TRACK, ... ]}}}
```
`TRACK` (Qobuz shape): `id`(trackId, int), `title`, `version`, `isrc`,
`duration`(s), `maximum_bit_depth`, `hires`(bool), `track_number`, `media_number`,
`performer`:{`id`,`name`}, `album`:{`id`(albumId, str), `title`,
`release_date_original`(YYYY-MM-DD), `image`:{`large`,`small`,`thumbnail`},
`tracks_count`?}. On failure: `{"success":false,"error":...}`.

## 2. Create download job — `POST /api/v2/downloads`  (Content-Type: application/json)
Body (from a chosen track):
```json
{"albumId": album.id, "trackId": str(track.id), "albumTitle": album.title,
 "artistName": performer.name, "artistId": str(performer.id),
 "coverUrl": album.image.large, "releaseDate": album.release_date_original,
 "tracksCount": 1, "quality": 27, "format": "FLAC", "bitrate": 320,
 "embedLyrics": true, "lyricsMode": "embed", "downloadBooklet": false,
 "attachCover": false, "zipName": "{artists} - {name}", "trackName": "{track} - {name}"}
```
→ `{"id": <uuid>, "accessToken": <uuid>, "status": "pending", ...}`.
(quality 27 = Qobuz hi-res FLAC. NOTE: in this capture the delivered file was `.mp3`
~12–18 MB — arcod may downgrade. Treat quality as *not guaranteed*: request FLAC,
accept whatever the prober reports. → Tier 2, `expected_lossless=False`.)

## 3. Poll — `GET /api/v2/downloads/<uuid>`
`status`: `pending → processing → downloading → completed` (also `failed` with
`error`). When `completed`: `{"status":"completed","progress":100,
"fileName":..,"fileSize":..,"downloadUrl":"https://dl.arcod.xyz/downloads/<uuid>/<file>"}`.

## 4. (optional) fresh signed URL — `POST /api/v2/downloads/<uuid>/url`  body `{}`
→ `{"downloadUrl":..,"fileName":..,"expiresIn":300}`. Use if the status
`downloadUrl` expired (300 s TTL).

## 5. Download — `GET <downloadUrl>` on `dl.arcod.xyz` → the audio bytes (stream to disk).

## Implementation notes
- No env creds; opt-in flag `CRATE_ARCOD=1`; base overridable `CRATE_ARCOD_BASE`.
- Selection reuses the shared gates (ISRC exact → title → artist → duration →
  match_score), same as qobuz/deezer. Metadata (title/artist/album/isrc/duration/
  track_no) flows to `DownloadOutcome`.
- The job is async: poll with a bounded deadline (Phase-0 timeout discipline);
  raise on `failed`/timeout so the cascade advances. Stream the file (Phase-0
  `stream_to`), `.part`+rename.
