"""
fetcher/candle_tracker.py
=========================
Tracker window 5 menit Polymarket BTC.

FIX KRITIS:
  - Beat price sekarang tracking source ("CHAINLINK" / "HYPERLIQUID" / "POLYMARKET_API")
  - Chainlink beat TIDAK bisa di-override oleh Hyperliquid
  - Tambah is_beat_reliable, beat_source, beat_set_elapsed
  - set_beat_from_chainlink() dan set_beat_from_hyperliquid() sebagai shortcut
"""

import time
from datetime import datetime, timezone
from typing import Optional


class CandleTracker:
    """
    Tracker window 5 menit Polymarket BTC.

    PENTING: Beat price HARUS dari Chainlink atau API Polymarket.
    Polymarket menggunakan harga Chainlink sebagai "price to beat".
    """

    WINDOW_DURATION      = 300  # 5 menit
    BEAT_RELIABLE_WINDOW = 30   # detik — jika beat di-set setelah ini, dianggap late

    def __init__(self):
        self.window_id:         Optional[str]   = None
        self.window_start:      Optional[float] = None
        self.window_end:        Optional[float] = None
        self.beat_price:        Optional[float] = None
        self.beat_source:       str             = "UNKNOWN"
        self.beat_set_elapsed:  float           = 999.0
        self.beat_set_at:       float           = 0.0
        self.is_new_window:     bool            = False
        self._last_window_id:   Optional[str]   = None
        self.update()

    def update(self) -> None:
        """Update state window berdasarkan waktu sekarang."""
        now          = time.time()
        window_start = (now // self.WINDOW_DURATION) * self.WINDOW_DURATION
        window_end   = window_start + self.WINDOW_DURATION

        dt        = datetime.fromtimestamp(window_start, tz=timezone.utc)
        window_id = dt.strftime("%Y%m%d-%H%M")

        self.is_new_window = (window_id != self._last_window_id)
        if self.is_new_window:
            self._last_window_id  = window_id
            self.beat_price       = None
            self.beat_source      = "UNKNOWN"
            self.beat_set_elapsed = 999.0
            self.beat_set_at      = 0.0

        self.window_id    = window_id
        self.window_start = window_start
        self.window_end   = window_end

    @property
    def remaining(self) -> float:
        return max(0.0, self.window_end - time.time())

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.window_start)

    @property
    def progress_pct(self) -> float:
        return min(1.0, self.elapsed / self.WINDOW_DURATION)

    # ── Beat price management ─────────────────────────────────

    def set_beat_price(self, price: float, source: str = "HYPERLIQUID") -> bool:
        """
        Set beat price untuk window saat ini.
        """
        if not price or price <= 0:
            return False

        # Sudah ada dari Chainlink atau API → jangan override
        if self.beat_price is not None and self.beat_source in ("CHAINLINK", "POLYMARKET_API"):
            return False

        self.beat_price       = price
        self.beat_source      = source
        self.beat_set_elapsed = self.elapsed
        self.beat_set_at      = time.time()
        return True

    def set_beat_from_chainlink(self, price: float) -> bool:
        """
        Set beat dari Chainlink — ini yang paling akurat dan sesuai Polymarket.
        """
        if not price or price <= 0:
            return False

        # Hanya API Polymarket yang boleh override Chainlink (kalau ada selisih API telat)
        if self.beat_price is not None and self.beat_source == "POLYMARKET_API":
            return False

        self.beat_price       = price
        self.beat_source      = "CHAINLINK"
        self.beat_set_elapsed = self.elapsed
        self.beat_set_at      = time.time()
        return True

    def set_beat_from_hyperliquid(self, price: float) -> bool:
        """
        Set beat dari Hyperliquid — fallback jika Chainlink tidak tersedia.
        """
        if self.beat_source in ("CHAINLINK", "POLYMARKET_API"):
            return False  # Pertahankan beat yang lebih akurat
        return self.set_beat_price(price, source="HYPERLIQUID")

    def set_beat_from_api(self, price: float) -> bool:
        """
        Set beat price RESMI dari API Polymarket (100% Akurat).
        Ini akan override semua harga tebakan sebelumnya.
        """
        if not price or price <= 0:
            return False
        
        self.beat_price       = price
        self.beat_source      = "POLYMARKET_API"
        self.beat_set_elapsed = self.elapsed
        self.beat_set_at      = time.time()
        return True

    @property
    def is_beat_reliable(self) -> bool:
        """
        True jika beat price kemungkinan akurat (sama dengan Polymarket).
        """
        if self.beat_price is None:
            return False
        if self.beat_source in ("CHAINLINK", "POLYMARKET_API"):
            return True
        if self.beat_source == "HYPERLIQUID" and self.beat_set_elapsed <= 10:
            return True
        return False

    @property
    def beat_warning(self) -> str:
        """Peringatan tentang akurasi beat price, kosong jika aman."""
        if self.beat_price is None:
            return "Beat price belum tersedia"
        if self.beat_source in ("CHAINLINK", "POLYMARKET_API"):
            return ""
        if self.beat_source == "HYPERLIQUID":
            if self.beat_set_elapsed <= 10:
                return ""
            return (
                f"Beat dari Hyperliquid (bukan Chainlink), "
                f"set t={self.beat_set_elapsed:.0f}s — mungkin beda ±$50 dari Polymarket"
            )
        return "Source beat price tidak diketahui"

    # ── Utility ───────────────────────────────────────────────

    def get_market_name(self) -> str:
        dt     = datetime.fromtimestamp(self.window_start, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(self.window_end, tz=timezone.utc)
        day    = str(dt.day)
        return (
            f"BTC Up or Down - {dt.strftime('%b')} {day}, "
            f"{dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')} UTC"
        )

    def progress_bar(self, width: int = 30) -> str:
        filled = int(self.progress_pct * width)
        return f"[{'█' * filled}{'░' * (width - filled)}]"

    def __repr__(self) -> str:
        src = self.beat_source
        rel = "✓" if self.is_beat_reliable else "⚠"
        return (
            f"CandleTracker(window={self.window_id}, "
            f"elapsed={self.elapsed:.0f}s, "
            f"beat={self.beat_price} [{src}{rel}])"
        )