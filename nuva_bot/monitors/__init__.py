"""Monitor registry — builds every enabled monitor from config.yaml."""

import logging

log = logging.getLogger("nuva.monitors")


def build_monitors(config) -> list:
    from .provenance import ProvenanceExplorerMonitor, ProvenanceGovMonitor
    from .ethereum import EtherscanMonitor
    from .market import CoinGeckoHashMonitor, DefiLlamaMonitor, OsmosisPoolMonitor
    from .webwatch import WebWatchMonitor
    from .github_dev import GitHubOrgsMonitor
    from .feeds import build_feed_monitors
    from .discord_watch import DiscordMonitor
    from .telegram_watch import TelegramChannelMonitor

    candidates = [
        ProvenanceExplorerMonitor(config),
        ProvenanceGovMonitor(config),
        EtherscanMonitor(config),
        CoinGeckoHashMonitor(config),
        DefiLlamaMonitor(config),
        OsmosisPoolMonitor(config),
        WebWatchMonitor(config),
        GitHubOrgsMonitor(config),
        *build_feed_monitors(config),
        DiscordMonitor(config),
        TelegramChannelMonitor(config),
    ]

    monitors = []
    for mon in candidates:
        if mon.enabled:
            monitors.append(mon)
        else:
            log.info("monitor %s disabled: %s", mon.name, mon.disabled_reason or "config")
    return monitors
