# Catalogue probe — measured stream properties

- **Gateway:** `live.corp8.cloud`
- **Catalogue:** `https://live.corp8.cloud/api/ingest`
- **RTSP :8554 reachable:** no — all cameras probed over HLS
- **Sample window:** 8s per camera, sequential
- **Commit:** `3170653fef07411a0724f69d2706e18a35aeda65` (working tree dirty)

## Why this exists

The catalogue reports `codec: ""`, `0x0` and `fps: 0.0` for most cameras, and
where it does declare an FPS that figure is a declaration, not a measurement.
§2.2 forbids using declared FPS for timing, so every property below marked
*measured* was derived from PTS deltas on real decoded frames.

**17/30 cameras produced frames.** 12 had a declared FPS that was absent or more than 15% from measured.

| id | location | via | codec | resolution (declared → measured) | fps (declared → measured) | TTFF | decoder msgs |
|---:|---|---|---|---|---|---:|---:|
| 1 | 01 Chiman bhai Bridge **FAIL: stream ended before probe window elapsed** | hls | — / — | — → — | — → — | — | 2 |
| 2 | 02 Janpath | hls | — / h264 | — → 1920x1080 | — → 29.8 ⚠ | 0.03s | 2 |
| 3 | 03 O.N.G.C. Office | hls | — / h264 | — → 1280x720 | — → 25.0 ⚠ | 0.02s | 3 |
| 4 | 04 Paldi Circle | hls | — / h264 | — → 1920x1080 | — → 25.0 ⚠ | 0.03s | 2 |
| 5 | 05 Visat teen Rasta | hls | — / h264 | — → 1920x1080 | — → 29.55 ⚠ | 0.05s | 2 |
| 6 | 06 Timbavadi gate-Junagadh | hls | hevc / hevc | 1920x1080 → 1920x1080 | 25.0 → 23.79 | 0.06s | 2 |
| 7 | 07 hero-showroom-gir-somnath **FAIL: stream ended before probe window elapsed** | hls | — / — | — → — | — → — | — | 7 |
| 8 | 08 majewadi-gate-junagadh **FAIL: stream ended before probe window elapsed** | hls | — / — | — → — | — → — | — | 39 |
| 9 | 09 new-bypass-near-by-circle-junagadh-2 **FAIL: stream ended before probe window elapsed** | hls | — / — | — → — | — → — | — | 13 |
| 10 | 10 char-chowk-road-2-junagadh **FAIL: stream ended before probe window elapsed** | hls | — / h264 | — → — | — → — | — | 3 |
| 11 | 11 dolatpara-junagadh | hls | — / h264 | — → 1920x1080 | — → — | 0.16s | 2 |
| 12 | 12 Tri Mandir Adalaj Tollnaka | hls | — / hevc | — → 1280x720 | — → 20.0 ⚠ | 0.09s | 2 |
| 13 | 13 CN Vidhyalaya | hls | h264 / h264 | 1920x1080 → 1920x1080 | 12.5 → 9.96 ⚠ | 0.06s | 2 |
| 14 | 14 Delight **FAIL: stream ended before probe window elapsed** | hls | h264 / h264 | 1920x1080 → — | 12.5 → — | — | 2 |
| 15 | 15 Suvidha park **FAIL: stream ended before probe window elapsed** | hls | h264 / — | 1920x1080 → — | 12.5 → — | — | 2 |
| 16 | 16 Visat P2 | hls | h264 / h264 | 1920x1080 → 1920x1080 | 12.5 → 9.96 ⚠ | 0.03s | 2 |
| 17 | 17 Rajkot Bus Port CCTV **FAIL: stream ended before probe window elapsed** | hls | hevc / — | 1920x1080 → — | 24.98 → — | — | 9 |
| 18 | 18 Rajkot CCTV **FAIL: stream ended before probe window elapsed** | hls | — / — | — → — | — → — | — | 19 |
| 19 | 19 KHAPARIA GRAM PANCHAYAT , TALUKA GANDEVI, DISTRICT NAVSARI **FAIL: stream ended before probe window elapsed** | hls | — / — | — → — | — → — | — | 9 |
| 20 | 20 Mohanpura **FAIL: stream ended before probe window elapsed** | hls | — / — | — → — | — → — | — | 13 |
| 21 | 23 Patan Dethali Char Rasta **FAIL: stream ended before probe window elapsed** | hls | — / — | — → — | — → — | — | 5 |
| 22 | 28 BK Mervada tran Rasta **FAIL: stream ended before probe window elapsed** | hls | hevc / — | 1920x1080 → — | 25.0 → — | — | 5 |
| 23 | 30 kheram | hls | h264 / h264 | 1280x720 → 1280x720 | 25.0 → 25.0 | 0.05s | 2 |
| 24 | 33 dehgam | hls | — / h264 | — → 960x576 | — → 12.0 ⚠ | 0.02s | 2 |
| 25 | 34 dhanori | hls | — / h264 | — → 1280x960 | — → 25.0 ⚠ | 0.09s | 2 |
| 26 | 35 TANKAL | hls | hevc / hevc | 2560x1440 → 2560x1440 | 13.35 → 25.0 ⚠ | 0.28s | 2 |
| 27 | 36 bilimora | hls | h264 / h264 | 1280x960 → 1280x960 | 24.86 → 25.0 | 0.06s | 2 |
| 28 | 37 bilimora | hls | — / h264 | — → 1280x960 | — → 25.0 ⚠ | 0.06s | 2 |
| 29 | 38 bilimora | hls | h264 / h264 | 1280x960 → 1280x960 | 24.78 → 25.0 | 0.09s | 2 |
| 30 | Gandhidham Rambaugh p2 | hls | — / h264 | — → 1920x1080 | — → 15.21 ⚠ | 0.09s | 2 |

## Decoder messages observed during join

§2.2: these are expected when attaching mid-stream before the first IDR and
must not be treated as fatal. They are reproduced verbatim (credentials
redacted) as evidence that they occurred and were absorbed.

### Camera 1 — 01 Chiman bhai Bridge

```
2026-08-27 02:27:58,672Z INFO    services.common.stream_client: opening camera=1 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:28:29,074Z WARNING services.common.stream_client: connect failed camera=1 attempt=1 retry_in=1.0s
```

### Camera 2 — 02 Janpath

```
2026-08-27 02:28:29,081Z INFO    services.common.stream_client: opening camera=2 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:28:30,523Z INFO    services.common.stream_client: camera=2 joined in 0.03s 1920x1080 declared_fps=30.0
```

### Camera 3 — 03 O.N.G.C. Office

```
2026-08-27 02:28:37,101Z INFO    services.common.stream_client: opening camera=3 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:28:39,060Z INFO    services.common.stream_client: camera=3 joined in 0.02s 1280x720 declared_fps=30.0
2026-08-27 02:28:42,634Z INFO    services.common.stream_client: SCENE_DISCONTINUITY camera=3 reason=scene_change session=856633fc92434369b88de15f42c8bcd0
```

### Camera 4 — 04 Paldi Circle

```
2026-08-27 02:28:45,110Z INFO    services.common.stream_client: opening camera=4 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:28:47,022Z INFO    services.common.stream_client: camera=4 joined in 0.03s 1920x1080 declared_fps=25.0
```

### Camera 5 — 05 Visat teen Rasta

```
2026-08-27 02:28:53,442Z INFO    services.common.stream_client: opening camera=5 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:28:55,601Z INFO    services.common.stream_client: camera=5 joined in 0.05s 1920x1080 declared_fps=30.0
```

### Camera 6 — 06 Timbavadi gate-Junagadh

```
2026-08-27 02:29:16,033Z INFO    services.common.stream_client: opening camera=6 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:29:17,595Z INFO    services.common.stream_client: camera=6 joined in 0.06s 1920x1080 declared_fps=25.0
```

### Camera 7 — 07 hero-showroom-gir-somnath

```
2026-08-27 02:29:24,102Z INFO    services.common.stream_client: opening camera=7 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:29:44,148Z WARNING services.common.stream_client: stream URL resolution failed camera=7: ('Connection aborted.', ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host', None, 10054, None))
2026-08-27 02:29:44,215Z WARNING services.common.stream_client: connect failed camera=7 attempt=1 retry_in=1.0s
2026-08-27 02:29:46,076Z WARNING services.common.stream_client: stream URL resolution failed camera=7: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/7/index.m3u8?cookieCheck=1
2026-08-27 02:29:46,079Z WARNING services.common.stream_client: connect failed camera=7 attempt=2 retry_in=1.0s
2026-08-27 02:29:47,340Z WARNING services.common.stream_client: stream URL resolution failed camera=7: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/7/index.m3u8?cookieCheck=1
2026-08-27 02:29:47,341Z WARNING services.common.stream_client: connect failed camera=7 attempt=3 retry_in=1.0s
```

### Camera 8 — 08 majewadi-gate-junagadh

```
2026-08-27 02:29:47,381Z INFO    services.common.stream_client: opening camera=8 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:29:47,497Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:47,498Z WARNING services.common.stream_client: connect failed camera=8 attempt=1 retry_in=1.0s
2026-08-27 02:29:48,732Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:48,734Z WARNING services.common.stream_client: connect failed camera=8 attempt=2 retry_in=1.0s
2026-08-27 02:29:49,991Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:49,992Z WARNING services.common.stream_client: connect failed camera=8 attempt=3 retry_in=1.0s
2026-08-27 02:29:51,285Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:51,285Z WARNING services.common.stream_client: connect failed camera=8 attempt=4 retry_in=1.0s
2026-08-27 02:29:52,559Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:52,560Z WARNING services.common.stream_client: connect failed camera=8 attempt=5 retry_in=1.0s
2026-08-27 02:29:53,734Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:53,735Z WARNING services.common.stream_client: connect failed camera=8 attempt=6 retry_in=1.0s
2026-08-27 02:29:55,028Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:55,028Z WARNING services.common.stream_client: connect failed camera=8 attempt=7 retry_in=1.0s
2026-08-27 02:29:56,310Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:56,311Z WARNING services.common.stream_client: connect failed camera=8 attempt=8 retry_in=1.0s
2026-08-27 02:29:57,612Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:57,612Z WARNING services.common.stream_client: connect failed camera=8 attempt=9 retry_in=1.0s
2026-08-27 02:29:58,743Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:58,744Z WARNING services.common.stream_client: connect failed camera=8 attempt=10 retry_in=1.0s
2026-08-27 02:29:59,989Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:29:59,990Z WARNING services.common.stream_client: connect failed camera=8 attempt=11 retry_in=1.0s
2026-08-27 02:30:01,244Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:30:01,245Z WARNING services.common.stream_client: connect failed camera=8 attempt=12 retry_in=1.0s
2026-08-27 02:30:02,520Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:30:02,521Z WARNING services.common.stream_client: connect failed camera=8 attempt=13 retry_in=1.0s
2026-08-27 02:30:03,783Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:30:03,783Z WARNING services.common.stream_client: connect failed camera=8 attempt=14 retry_in=1.0s
2026-08-27 02:30:04,923Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:30:04,925Z WARNING services.common.stream_client: connect failed camera=8 attempt=15 retry_in=1.0s
2026-08-27 02:30:06,189Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:30:06,190Z WARNING services.common.stream_client: connect failed camera=8 attempt=16 retry_in=1.0s
2026-08-27 02:30:07,438Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:30:07,440Z WARNING services.common.stream_client: connect failed camera=8 attempt=17 retry_in=1.0s
2026-08-27 02:30:08,724Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:30:08,724Z WARNING services.common.stream_client: connect failed camera=8 attempt=18 retry_in=1.0s
2026-08-27 02:30:09,889Z WARNING services.common.stream_client: stream URL resolution failed camera=8: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/8/index.m3u8?cookieCheck=1
2026-08-27 02:30:09,890Z WARNING services.common.stream_client: connect failed camera=8 attempt=19 retry_in=1.0s
```

### Camera 9 — 09 new-bypass-near-by-circle-junagadh-2

```
2026-08-27 02:30:10,400Z INFO    services.common.stream_client: opening camera=9 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:30:10,514Z WARNING services.common.stream_client: stream URL resolution failed camera=9: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/9/index.m3u8?cookieCheck=1
2026-08-27 02:30:10,514Z WARNING services.common.stream_client: connect failed camera=9 attempt=1 retry_in=1.0s
2026-08-27 02:30:11,786Z WARNING services.common.stream_client: stream URL resolution failed camera=9: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/9/index.m3u8?cookieCheck=1
2026-08-27 02:30:11,788Z WARNING services.common.stream_client: connect failed camera=9 attempt=2 retry_in=1.0s
2026-08-27 02:30:12,950Z WARNING services.common.stream_client: stream URL resolution failed camera=9: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/9/index.m3u8?cookieCheck=1
2026-08-27 02:30:12,952Z WARNING services.common.stream_client: connect failed camera=9 attempt=3 retry_in=1.0s
2026-08-27 02:30:14,079Z WARNING services.common.stream_client: stream URL resolution failed camera=9: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/9/index.m3u8?cookieCheck=1
2026-08-27 02:30:14,080Z WARNING services.common.stream_client: connect failed camera=9 attempt=4 retry_in=1.0s
2026-08-27 02:30:15,362Z WARNING services.common.stream_client: stream URL resolution failed camera=9: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/9/index.m3u8?cookieCheck=1
2026-08-27 02:30:15,363Z WARNING services.common.stream_client: connect failed camera=9 attempt=5 retry_in=1.0s
2026-08-27 02:30:35,875Z WARNING services.common.stream_client: stream URL resolution failed camera=9: ('Connection aborted.', ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host', None, 10054, None))
2026-08-27 02:30:35,875Z WARNING services.common.stream_client: connect failed camera=9 attempt=6 retry_in=1.0s
```

### Camera 10 — 10 char-chowk-road-2-junagadh

```
2026-08-27 02:30:35,883Z INFO    services.common.stream_client: opening camera=10 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:30:55,998Z WARNING services.common.stream_client: stream URL resolution failed camera=10: HTTPSConnectionPool(host='live.corp8.cloud', port=443): Read timed out. (read timeout=20.0)
2026-08-27 02:30:55,999Z WARNING services.common.stream_client: connect failed camera=10 attempt=1 retry_in=1.0s
```

### Camera 11 — 11 dolatpara-junagadh

```
2026-08-27 02:30:59,842Z INFO    services.common.stream_client: opening camera=11 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:31:13,204Z INFO    services.common.stream_client: camera=11 joined in 0.16s 1920x1080 declared_fps=25.0
```

### Camera 12 — 12 Tri Mandir Adalaj Tollnaka

```
2026-08-27 02:31:13,283Z INFO    services.common.stream_client: opening camera=12 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:31:15,268Z INFO    services.common.stream_client: camera=12 joined in 0.09s 1280x720 declared_fps=20.0
```

### Camera 13 — 13 CN Vidhyalaya

```
2026-08-27 02:31:21,811Z INFO    services.common.stream_client: opening camera=13 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:31:24,119Z INFO    services.common.stream_client: camera=13 joined in 0.06s 1920x1080 declared_fps=10.0
```

### Camera 14 — 14 Delight

```
2026-08-27 02:31:29,844Z INFO    services.common.stream_client: opening camera=14 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:31:49,607Z WARNING services.common.stream_client: connect failed camera=14 attempt=1 retry_in=1.0s
```

### Camera 15 — 15 Suvidha park

```
2026-08-27 02:31:53,495Z INFO    services.common.stream_client: opening camera=15 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:32:16,412Z WARNING services.common.stream_client: connect failed camera=15 attempt=1 retry_in=1.0s
```

### Camera 16 — 16 Visat P2

```
2026-08-27 02:32:16,513Z INFO    services.common.stream_client: opening camera=16 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:32:18,388Z INFO    services.common.stream_client: camera=16 joined in 0.03s 1920x1080 declared_fps=10.0
```

### Camera 17 — 17 Rajkot Bus Port CCTV

```
2026-08-27 02:32:25,408Z INFO    services.common.stream_client: opening camera=17 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:32:26,258Z WARNING services.common.stream_client: stream URL resolution failed camera=17: 500 Server Error: Internal Server Error for url: https://live.corp8.cloud/live/stream/17/index.m3u8?cookieCheck=1
2026-08-27 02:32:26,258Z WARNING services.common.stream_client: connect failed camera=17 attempt=1 retry_in=1.0s
2026-08-27 02:32:36,248Z WARNING services.common.stream_client: stream URL resolution failed camera=17: 500 Server Error: Internal Server Error for url: https://live.corp8.cloud/live/stream/17/index.m3u8?cookieCheck=1
2026-08-27 02:32:36,250Z WARNING services.common.stream_client: connect failed camera=17 attempt=2 retry_in=1.0s
2026-08-27 02:32:46,289Z WARNING services.common.stream_client: stream URL resolution failed camera=17: 500 Server Error: Internal Server Error for url: https://live.corp8.cloud/live/stream/17/index.m3u8?cookieCheck=1
2026-08-27 02:32:46,290Z WARNING services.common.stream_client: connect failed camera=17 attempt=3 retry_in=1.0s
2026-08-27 02:32:56,250Z WARNING services.common.stream_client: stream URL resolution failed camera=17: 500 Server Error: Internal Server Error for url: https://live.corp8.cloud/live/stream/17/index.m3u8?cookieCheck=1
2026-08-27 02:32:56,251Z WARNING services.common.stream_client: connect failed camera=17 attempt=4 retry_in=1.0s
```

### Camera 18 — 18 Rajkot CCTV

```
2026-08-27 02:32:56,262Z INFO    services.common.stream_client: opening camera=18 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:33:04,760Z WARNING services.common.stream_client: stream URL resolution failed camera=18: 500 Server Error: Internal Server Error for url: https://live.corp8.cloud/live/stream/18/index.m3u8?cookieCheck=1
2026-08-27 02:33:04,762Z WARNING services.common.stream_client: connect failed camera=18 attempt=1 retry_in=1.0s
2026-08-27 02:33:10,093Z WARNING services.common.stream_client: stream URL resolution failed camera=18: 502 Server Error: Bad Gateway for url: https://live.corp8.cloud/live/stream/18/index.m3u8?cookieCheck=1
2026-08-27 02:33:10,094Z WARNING services.common.stream_client: connect failed camera=18 attempt=2 retry_in=1.0s
2026-08-27 02:33:11,388Z WARNING services.common.stream_client: stream URL resolution failed camera=18: 502 Server Error: Bad Gateway for url: https://live.corp8.cloud/live/stream/18/index.m3u8?cookieCheck=1
2026-08-27 02:33:11,388Z WARNING services.common.stream_client: connect failed camera=18 attempt=3 retry_in=1.0s
2026-08-27 02:33:12,658Z WARNING services.common.stream_client: stream URL resolution failed camera=18: 502 Server Error: Bad Gateway for url: https://live.corp8.cloud/live/stream/18/index.m3u8?cookieCheck=1
2026-08-27 02:33:12,659Z WARNING services.common.stream_client: connect failed camera=18 attempt=4 retry_in=1.0s
2026-08-27 02:33:13,920Z WARNING services.common.stream_client: stream URL resolution failed camera=18: 502 Server Error: Bad Gateway for url: https://live.corp8.cloud/live/stream/18/index.m3u8?cookieCheck=1
2026-08-27 02:33:13,921Z WARNING services.common.stream_client: connect failed camera=18 attempt=5 retry_in=1.0s
2026-08-27 02:33:15,176Z WARNING services.common.stream_client: stream URL resolution failed camera=18: 502 Server Error: Bad Gateway for url: https://live.corp8.cloud/live/stream/18/index.m3u8?cookieCheck=1
2026-08-27 02:33:15,177Z WARNING services.common.stream_client: connect failed camera=18 attempt=6 retry_in=1.0s
2026-08-27 02:33:16,325Z WARNING services.common.stream_client: stream URL resolution failed camera=18: 502 Server Error: Bad Gateway for url: https://live.corp8.cloud/live/stream/18/index.m3u8?cookieCheck=1
2026-08-27 02:33:16,325Z WARNING services.common.stream_client: connect failed camera=18 attempt=7 retry_in=1.0s
2026-08-27 02:33:17,635Z WARNING services.common.stream_client: stream URL resolution failed camera=18: 502 Server Error: Bad Gateway for url: https://live.corp8.cloud/live/stream/18/index.m3u8?cookieCheck=1
2026-08-27 02:33:17,636Z WARNING services.common.stream_client: connect failed camera=18 attempt=8 retry_in=1.0s
2026-08-27 02:33:18,933Z WARNING services.common.stream_client: stream URL resolution failed camera=18: 502 Server Error: Bad Gateway for url: https://live.corp8.cloud/live/stream/18/index.m3u8?cookieCheck=1
2026-08-27 02:33:18,933Z WARNING services.common.stream_client: connect failed camera=18 attempt=9 retry_in=1.0s
```

### Camera 19 — 19 KHAPARIA GRAM PANCHAYAT , TALUKA GANDEVI, DISTRICT NAVSARI

```
2026-08-27 02:33:19,278Z INFO    services.common.stream_client: opening camera=19 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:33:19,475Z WARNING services.common.stream_client: stream URL resolution failed camera=19: 502 Server Error: Bad Gateway for url: https://live.corp8.cloud/live/stream/19/index.m3u8?cookieCheck=1
2026-08-27 02:33:19,475Z WARNING services.common.stream_client: connect failed camera=19 attempt=1 retry_in=1.0s
2026-08-27 02:33:20,705Z WARNING services.common.stream_client: stream URL resolution failed camera=19: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/19/index.m3u8?cookieCheck=1
2026-08-27 02:33:20,706Z WARNING services.common.stream_client: connect failed camera=19 attempt=2 retry_in=1.0s
2026-08-27 02:33:21,966Z WARNING services.common.stream_client: stream URL resolution failed camera=19: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/19/index.m3u8?cookieCheck=1
2026-08-27 02:33:21,966Z WARNING services.common.stream_client: connect failed camera=19 attempt=3 retry_in=1.0s
2026-08-27 02:33:43,052Z WARNING services.common.stream_client: stream URL resolution failed camera=19: HTTPSConnectionPool(host='live.corp8.cloud', port=443): Read timed out. (read timeout=20.0)
2026-08-27 02:33:43,053Z WARNING services.common.stream_client: connect failed camera=19 attempt=4 retry_in=1.0s
```

### Camera 20 — 20 Mohanpura

```
2026-08-27 02:33:43,062Z INFO    services.common.stream_client: opening camera=20 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:33:43,323Z WARNING services.common.stream_client: stream URL resolution failed camera=20: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/20/index.m3u8?cookieCheck=1
2026-08-27 02:33:43,323Z WARNING services.common.stream_client: connect failed camera=20 attempt=1 retry_in=1.0s
2026-08-27 02:33:44,467Z WARNING services.common.stream_client: stream URL resolution failed camera=20: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/20/index.m3u8?cookieCheck=1
2026-08-27 02:33:44,468Z WARNING services.common.stream_client: connect failed camera=20 attempt=2 retry_in=1.0s
2026-08-27 02:34:01,814Z WARNING services.common.stream_client: stream URL resolution failed camera=20: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/20/index.m3u8?cookieCheck=1
2026-08-27 02:34:01,816Z WARNING services.common.stream_client: connect failed camera=20 attempt=3 retry_in=1.0s
2026-08-27 02:34:03,057Z WARNING services.common.stream_client: stream URL resolution failed camera=20: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/20/index.m3u8?cookieCheck=1
2026-08-27 02:34:03,058Z WARNING services.common.stream_client: connect failed camera=20 attempt=4 retry_in=1.0s
2026-08-27 02:34:04,276Z WARNING services.common.stream_client: stream URL resolution failed camera=20: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/20/index.m3u8?cookieCheck=1
2026-08-27 02:34:04,277Z WARNING services.common.stream_client: connect failed camera=20 attempt=5 retry_in=1.0s
2026-08-27 02:34:05,473Z WARNING services.common.stream_client: stream URL resolution failed camera=20: 404 Client Error: Not Found for url: https://live.corp8.cloud/live/stream/20/index.m3u8?cookieCheck=1
2026-08-27 02:34:05,473Z WARNING services.common.stream_client: connect failed camera=20 attempt=6 retry_in=1.0s
```

### Camera 21 — 23 Patan Dethali Char Rasta

```
2026-08-27 02:34:06,082Z INFO    services.common.stream_client: opening camera=21 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:34:25,504Z WARNING services.common.stream_client: stream URL resolution failed camera=21: ('Connection aborted.', ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host', None, 10054, None))
2026-08-27 02:34:25,509Z WARNING services.common.stream_client: connect failed camera=21 attempt=1 retry_in=1.0s
2026-08-27 02:34:46,587Z WARNING services.common.stream_client: stream URL resolution failed camera=21: HTTPSConnectionPool(host='live.corp8.cloud', port=443): Read timed out. (read timeout=20.0)
2026-08-27 02:34:46,587Z WARNING services.common.stream_client: connect failed camera=21 attempt=2 retry_in=1.0s
```

### Camera 22 — 28 BK Mervada tran Rasta

```
2026-08-27 02:34:46,592Z INFO    services.common.stream_client: opening camera=22 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:34:50,536Z WARNING services.common.stream_client: stream URL resolution failed camera=22: 500 Server Error: Internal Server Error for url: https://live.corp8.cloud/live/stream/22/index.m3u8?cookieCheck=1
2026-08-27 02:34:50,536Z WARNING services.common.stream_client: connect failed camera=22 attempt=1 retry_in=1.0s
2026-08-27 02:35:11,671Z WARNING services.common.stream_client: stream URL resolution failed camera=22: HTTPSConnectionPool(host='live.corp8.cloud', port=443): Read timed out. (read timeout=20.0)
2026-08-27 02:35:11,672Z WARNING services.common.stream_client: connect failed camera=22 attempt=2 retry_in=1.0s
```

### Camera 23 — 30 kheram

```
2026-08-27 02:35:11,681Z INFO    services.common.stream_client: opening camera=23 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:35:13,778Z INFO    services.common.stream_client: camera=23 joined in 0.05s 1280x720 declared_fps=25.0
```

### Camera 24 — 33 dehgam

```
2026-08-27 02:35:45,090Z INFO    services.common.stream_client: opening camera=24 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:35:46,556Z INFO    services.common.stream_client: camera=24 joined in 0.02s 960x576 declared_fps=12.0
```

### Camera 25 — 34 dhanori

```
2026-08-27 02:36:07,027Z INFO    services.common.stream_client: opening camera=25 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:36:08,711Z INFO    services.common.stream_client: camera=25 joined in 0.09s 1280x960 declared_fps=25.0
```

### Camera 26 — 35 TANKAL

```
2026-08-27 02:36:15,048Z INFO    services.common.stream_client: opening camera=26 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:36:18,673Z INFO    services.common.stream_client: camera=26 joined in 0.28s 2560x1440 declared_fps=25.0
```

### Camera 27 — 36 bilimora

```
2026-08-27 02:36:23,116Z INFO    services.common.stream_client: opening camera=27 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:36:24,524Z INFO    services.common.stream_client: camera=27 joined in 0.06s 1280x960 declared_fps=25.0
```

### Camera 28 — 37 bilimora

```
2026-08-27 02:36:31,147Z INFO    services.common.stream_client: opening camera=28 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:36:32,758Z INFO    services.common.stream_client: camera=28 joined in 0.06s 1280x960 declared_fps=25.0
```

### Camera 29 — 38 bilimora

```
2026-08-27 02:36:39,194Z INFO    services.common.stream_client: opening camera=29 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:36:40,706Z INFO    services.common.stream_client: camera=29 joined in 0.09s 1280x960 declared_fps=25.0
```

### Camera 30 — Gandhidham Rambaugh p2

```
2026-08-27 02:36:50,999Z INFO    services.common.stream_client: opening camera=30 transport=hls options=rtsp_transport;tcp|timeout;15000000|max_delay;500000|reorder_queue_size;0|rw_timeout;20000000|reconnect;1|reconnect_streamed;1|live_start_index;-1
2026-08-27 02:36:52,950Z INFO    services.common.stream_client: camera=30 joined in 0.09s 1920x1080 declared_fps=25.0
```

