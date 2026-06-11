# Source Validation — 2026-06-11

Live M1 validation was run from the local execution repo with network access:

```bash
python3 -B -m pipeline.run_m1 --timeout 20
```

Baseline run:

- Run id: `20260611T055058Z-live`
- Status: `partial`
- Include health: 15/19 by fetch status, but 14/19 by usable non-empty item output.
- Deduped items: 237

Failed include sources:

| source_id | result |
|---|---|
| `google-deepmind-blog` | TLS EOF while fetching `https://deepmind.google/blog/rss.xml` |
| `google-ai-blog` | TLS handshake timeout while fetching `https://blog.google/technology/ai/rss/` |
| `microsoft-ai-blog` | `https://blogs.microsoft.com/ai/feed/` returned HTTP 410 |
| `ai-news-buttondown` | TLS handshake timeout |

Suspicious successful sources:

| source_id | issue | action |
|---|---|---|
| `nvidia-ai-blog` | Feed fetched but produced zero entries. | Moved to `probe`. |
| `infoq-ai` | Feed returned stale 2019 items. | Moved to `probe`. |

Immediate config changes:

- Replaced Microsoft AI feed with `https://news.microsoft.com/source/topics/ai/feed/`, which returned RSS with 10 items in targeted validation.
- Moved `nvidia-ai-blog` and `infoq-ai` from `include` to `probe`.
- Updated health calculation so include sources count as healthy only when they fetch successfully and yield at least one normalized item.

Corrected run:

- Run id: `20260611T060023Z-live`
- Status: `partial`
- Include health: 16/17 usable include sources, 94.12%.
- Deduped items: 254
- Gate result: passed.

Only failed include source in the corrected run:

| source_id | result |
|---|---|
| `hnrss-llm` | HTTP 502 from HNRSS. `hnrss-ai` succeeded in the same run, so treat this as likely upstream/transient unless repeated. |

Quality notes:

- OpenAI, Google DeepMind, Google AI, Microsoft AI News, GitHub, QbitAI, HNRSS, arXiv, Simon Willison, Latent Space, The Decoder, TechCrunch AI, MIT Technology Review AI, AI News/Buttondown, and NVIDIA Deep Learning produced plausible metadata-level items in at least one live run.
- Excerpts stayed within the 200-character cap.
- No raw HTML was stored.
- HNRSS and arXiv are high-volume/noisy by design and will need M2 topic filtering and clustering before the Portal feels calm.
