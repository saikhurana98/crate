-- The Spotify client used to follow `next` pagination links with an empty params
-- dict, which httpx treats as "replace the query string", so every page after
-- the first was page 1 again: the playlist list, Liked Songs and long playlists
-- were truncated. Forget the stored snapshots so the next library_sync refetches
-- every playlist's items in full.
UPDATE sync_playlists SET snapshot_id = NULL;
