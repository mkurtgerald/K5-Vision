# Stage One bounded live-scene retry

The current Stage One physical witness reached authenticated live presentation and completed reviewed analytics inference without provider failures, but the bounded live window produced no accepted person detections and therefore no rendered boxes.

The repair must preserve the existing acceptance bar: at least one real K5-rendered box, zero analytics failures, unchanged detector and renderer confidence, source-free retained evidence, and no private media retention.

Implement a bounded retry only when a live attempt is otherwise healthy and yields zero boxes. Fail immediately for live-path errors, analytics failures, malformed receipts, or any weakened security/privacy condition. Cap retries so the self-hosted runner remains bounded.
