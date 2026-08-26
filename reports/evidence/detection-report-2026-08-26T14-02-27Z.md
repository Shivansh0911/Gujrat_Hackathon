# Detected vehicles and number plates

Gateway status: the Government-provided feed at live.corp8.cloud returned HTTP 502 on every media playlist throughout the build, so no detections in this report originate from it. Its catalogue endpoint remained reachable. The fault was reported to the organisers; see docs/SUPPORT_QUERY.md. Every row here comes from our own-feed footage processed through the identical pipeline.

| metric | value |
|---|---:|
| Detections | 56 |
| Cameras | 4 |
| Parse as Indian registrations | 32 |
| Distinct plates | 8 |
| From the Government feed | 0 |

Full rows in the accompanying CSV and PDF. Unparsed reads are included: a
detection that did not match plate grammar is still evidence a vehicle
passed a camera at a time.

