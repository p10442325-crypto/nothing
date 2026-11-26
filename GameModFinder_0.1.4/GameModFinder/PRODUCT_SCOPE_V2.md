# Pawchive Mod Finder v2 — Product Scope

## Product goal
Help users discover Pawchive creators, follow them, browse their posts, and download selected files. This is a discovery and downloader app, not a mod manager.

## Core navigation
1. Following — newest posts from followed creators; default startup screen.
2. Discover creators — search by creator ID or paste a Pawchive profile URL.
3. Creators — followed creator list and each creator's post timeline.
4. Explore — browse/search Pawchive posts, with creator and available metadata filters.
5. Downloads — active/completed/failed downloads and open destination.

## Creator identity
Use the stable pair `(service, creator_id)`. URL intake extracts both values. Manual ID intake requires selecting the Pawchive service. Verify the profile before saving and prevent duplicates.

## Content model
Keep creators, posts, media/attachments, follows, sync checkpoints, and download records separate. A Pawchive post is not automatically a game mod. Do not add creator quality scoring or automatic mod classification in v1.

## Sync
- Default interval: 60 minutes.
- Manual refresh always available.
- Incremental per-creator sync with pagination and duplicate protection.
- Display last successful sync and errors.
- Respect rate limits and use bounded requests.

## Downloads
- Download selected post files in-app.
- Show file name, size when available, progress, and errors.
- Support safe ZIP extraction.
- Prefer a per-game destination; allow an optional common destination as fallback.
- Never overwrite silently. Prevent archive path traversal and unsafe extraction.
- Do not execute downloaded files.
- Do not manage installed mods, activation, updates, removal, or game launching.

## V1 implementation order
1. Validate Pawchive creator/profile/post API responses and real file download behavior.
2. Implement creator lookup, URL parsing, follow/unfollow, and local persistence.
3. Implement creator timelines, Following feed, and Explore.
4. Implement hourly/manual sync with pagination and deduplication.
5. Implement download queue, safe extraction, and destination settings.
6. Add automated tests and Windows packaging.

## API validation notes
The public API base documented by the Pawchive client ecosystem is `https://pawchive.pw/api/v1`. Common public routes include `/creators`, `/{service}/user/{id}/profile`, `/{service}/user/{id}`, `/posts`, and `/{service}/user/{id}/post/{post_id}`. Treat these as candidate routes until response shapes and pagination are verified against live responses. File downloads use Pawchive's dedicated file host; some content may require authorization or may not be downloadable.

## Out of scope
- Multi-platform aggregation
- Creator ranking/reputation scoring
- Automatic mod-vs-non-mod classification
- Mod installation management after extraction
- Enabling/disabling, updating, or uninstalling mods
