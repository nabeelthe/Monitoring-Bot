"""Signal confidence scoring.

Every event gets a 0-100 confidence score and a routing priority
(critical / high / medium / low / ignore) computed from:
  · source credibility (per-layer weights, config-editable)
  · route-map priority class (★ always / ● escalate / ■ kw)
  · auto-escalation keyword hits
  · cross-layer corroboration (independent confirmations in the story window)
"""

from .events import Event

DEFAULT_SOURCE_WEIGHTS = {
    "onchain": 90,   # blockchain data is ground truth
    "ethereum": 88,
    "market": 82,
    "token": 80,     # official pages
    "dev": 76,       # signed commits / releases
    "blog": 65,
    "news": 58,
    "social": 45,    # anyone can tweet
    "system": 70,
}

# tags that count as meaningful corroboration across layers
SIGNAL_TAGS = (
    "tge", "airdrop", "listing", "mainnet", "genesis", "mint", "burn",
    "governance", "upgrade", "tvl", "price", "volume", "vault", "release",
    "exploit", "hack", "depeg", "audit", "whale", "liquidity", "partnership",
    "snapshot", "sale",
)


def extract_tags(title: str, body: str, layer: str) -> list[str]:
    text = f"{title} {body}".lower()
    tags = [t for t in SIGNAL_TAGS if t in text]
    tags.append(layer)
    return tags


class ConfidenceScorer:
    def __init__(self, config):
        weights = dict(DEFAULT_SOURCE_WEIGHTS)
        weights.update(config.section("intelligence.source_weights"))
        self.weights = {k: int(v) for k, v in weights.items()}

    def score(self, ev: Event, corroborating: int = 0) -> Event:
        """Mutates ev: sets confidence + priority. `corroborating` = number of
        other-layer events in the same story window."""
        conf = self.weights.get(ev.layer, 55)

        if ev.priority_class == "always":
            conf += 10
        elif ev.priority_class == "kw":
            conf -= 5

        conf += min(len(ev.escalation_hits) * 7, 21)
        conf += min(corroborating * 6, 18)

        ev.confidence = max(5, min(conf, 99))
        ev.priority = self._priority(ev)
        return ev

    @staticmethod
    def _priority(ev: Event) -> str:
        esc = bool(ev.escalation_hits)
        always = ev.priority_class == "always"
        if ev.confidence >= 85 and (esc or always):
            return "critical"
        if ev.confidence >= 72 or (esc and ev.confidence >= 60):
            return "high"
        if ev.confidence >= 52 or always:
            return "medium"
        if ev.confidence >= 32:
            return "low"
        return "ignore"
