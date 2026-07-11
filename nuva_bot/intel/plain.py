"""Plain-language layer — turns crypto jargon into one sentence anyone understands.

Used for the "🗣 In plain terms" line on every alert and as the rule-based
analyst's plain summary. Keyword-driven so it works with zero AI dependency.
"""

import re

# Ordered most-specific → most-general; first match wins.
_RULES: list[tuple[str, str]] = [
    # security first — always most important
    (r"exploit|hack|drain|attack|phish", "⚠️ Possible security incident — money could be at risk. Treat with caution and verify before acting."),
    (r"depeg", "⚠️ A token that's supposed to hold a steady value may have slipped off it — a warning sign for the protocol."),
    (r"rug|scam|fake", "⚠️ Possible scam or fake announcement — do not click links or send funds until it's confirmed real."),
    # cross-source anomaly
    (r"unusual surge|signals in the last hour", "📡 Far more activity than normal is being detected — this often happens right before big news breaks."),
    # token / launch events
    (r"\btge\b|token generation", "🚀 Signs the token launch (TGE) is near — this is when NUVA first becomes buyable/tradable."),
    (r"airdrop", "🎁 A possible free token giveaway (airdrop) to holders — worth checking if you qualify."),
    (r"listing|listed on", "📈 A token may be getting added to an exchange — this often moves the price a lot."),
    (r"genesis pass|nuva points", "🎟️ Update to the pre-launch rewards program (Genesis Pass / points) that affects future airdrop size."),
    (r"public sale|series a|raise|funding round", "💰 A fundraising event — new money coming into the project."),
    (r"partnership|integration|collaborat", "🤝 The project is teaming up with another company or protocol."),
    # on-chain structural (match 'mint' inside proto types like MsgMint too)
    (r"mint", "🟢 New tokens were created on the blockchain (the supply went up)."),
    (r"burn", "🔴 Tokens were permanently destroyed (the supply went down)."),
    (r"nuheloc|nuylds|nvasset|vault", "🏦 Activity in a Nuva asset vault — the real-world-asset products that back the NUVA ecosystem."),
    (r"marker", "🔖 A new asset was registered on the Provenance blockchain."),
    (r"scope|metadata", "📝 Records/data were written on-chain (often tied to real-world assets)."),
    (r"proposal", "🗳️ A new community vote (governance proposal) was opened — the outcome can change how the network works."),
    (r"upgrade", "🔧 The blockchain is scheduled for a software upgrade."),
    (r"transfer|send", "💸 A large amount of tokens moved between wallets."),
    # market
    (r"volume spike", "📊 Trading activity suddenly jumped — a lot more buying/selling than usual."),
    (r"big move|price", "📈 The price of HASH made a notable move."),
    (r"tvl", "🏦 The total amount of money deposited in the protocol changed meaningfully."),
    (r"liquidity", "💧 The amount of money available to trade in a pool changed — affects how easily you can buy/sell."),
    # dev
    (r"release|version|v\d", "🛠️ The developers shipped a new version of their software."),
    (r"commit|repo|deploy", "👩‍💻 Developer activity — new code was published."),
    # social / news
    (r"@|tweet|posted|x \(twitter\)", "💬 An official or ecosystem account posted something worth noting."),
    (r"reddit|youtube|discord|telegram|medium|mirror", "💬 New community/media post mentioning Nuva or Provenance."),
    (r"news|article|coindesk|block|cointelegraph|decrypt", "🗞️ The press wrote an article about Nuva or Provenance."),
]

_COMPILED = [(re.compile(pat, re.I), msg) for pat, msg in _RULES]


def humanize(layer: str, title: str, body: str = "") -> str:
    """Return one plain-English sentence describing what happened."""
    text = f"{title} {body}"
    for rx, msg in _COMPILED:
        if rx.search(text):
            return msg
    # layer-based fallback so we always say *something* useful
    return {
        "onchain": "⛓️ Something happened on the Provenance blockchain worth noting.",
        "ethereum": "🪙 On-chain activity involving the NUVA token contracts on Ethereum.",
        "market": "📈 A market data point for HASH changed.",
        "token": "🚀 A change on an official Nuva token/launch page.",
        "dev": "🛠️ Developer activity in the project's code.",
        "blog": "📰 A new post on an official blog or docs site.",
        "social": "💬 New social-media activity about Nuva/Provenance.",
        "news": "🗞️ A news mention of Nuva/Provenance.",
    }.get(layer, "🔔 A monitored source reported new activity.")
