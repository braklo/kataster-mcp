"""Access policy: pacing (at least 3 s between ESKN requests) and the daily cap on owner lookups.

The public entry point always uses PUBLIC_POLICY. There is deliberately no config
file or environment variable that changes it.
"""

from dataclasses import dataclass

ESKN_HOST = "kataster.skgeodesy.sk"


@dataclass(frozen=True)
class Policy:
    # Pause between requests to ESKN_HOST, plus random jitter.
    eskn_pause_s: float = 3.0
    eskn_jitter_s: float = 1.0
    # Pause between requests to any other host.
    other_pause_s: float = 1.0
    # Parcels per calendar day (Europe/Bratislava) whose owners were fetched from the portal.
    owners_daily_hard: int = 50
    # When set, reaching it requires explicit confirmation before continuing.
    owners_daily_soft: int | None = None

    def pause_for(self, host: str) -> tuple[float, float]:
        if host == ESKN_HOST:
            return self.eskn_pause_s, self.eskn_jitter_s
        return self.other_pause_s, 0.0


PUBLIC_POLICY = Policy()
