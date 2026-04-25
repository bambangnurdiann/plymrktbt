"""
fetcher/candle_tracker.py
=========================
Tracker window 5 menit Polymarket BTC.

FIX KRITIS:
  - Beat price sekarang tracking source ("CHAINLINK" / "HYPERLIQUID")
  - Chainlink beat TIDAK bisa di-override oleh Hyperliquid
  - Tambah is_beat_reliable, beat_source, beat_set_elapsed
  - set_beat_from_chainlink() dan set_beat_from_hyperliquid() sebagai shortcut

Root cause bug WIN/LOSS mismatch:
  Bot sebelumnya pakai harga Hyperliquid sebagai beat price.
  Hyperliquid = perpetual futures, bisa beda $20-50 dari Chainlink spot.
  Polymarket resolve pakai Chainlink. Akibatnya hasil resolve berbeda.
"""

import time
from datetime import datetime, timezone
from typing import Optional


class CandleTracker:
    """
    Tracker window 5 menit Polymarket BTC.

    PENTING: Beat price HARUS dari Chainlink, bukan Hyperliquid.
    Polymarket menggunakan harga Chainlink sebagai "price to beat".

    Attributes:
        window_id         : str   — ID unik window (format: "YYYYMMDD-HHMM")
        window_start      : float — Unix timestamp saat window dimulai
        window_end        : float — Unix timestamp saat window berakhir
        beat_price        : float — Harga Chainlink saat window dibuka
        beat_source       : str   — "CHAINLINK" / "HYPERLIQUID" / "UNKNOWN"
        beat_set_elapsed  : float — elapsed detik saat beat di-set
        is_new_window     : bool  — True jika window baru saja berganti
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

        Rules:
          1. Jika sudah ada dari CHAINLINK → tidak bisa di-override apapun
          2. Jika belum ada → terima dari sumber manapun
          3. Catat elapsed saat beat di-set untuk keperluan diagnostik

        Returns True jika berhasil di-set, False jika ditolak.
        """
        if not price or price <= 0:
            return False

        # Sudah ada dari Chainlink → jangan override
        if self.beat_price is not None and self.beat_source == "CHAINLINK":
            return False

        self.beat_price       = price
        self.beat_source      = source
        self.beat_set_elapsed = self.elapsed
        self.beat_set_at      = time.time()
        return True

    def set_beat_from_chainlink(self, price: float) -> bool:
        """
        Set beat dari Chainlink — ini yang paling akurat dan sesuai Polymarket.
        Override beat Hyperliquid jika sudah ada.
        """
        if not price or price <= 0:
            return False

        # Chainlink boleh override Hyperliquid (lebih akurat)
        if self.beat_price is not None and self.beat_source == "CHAINLINK":
            return False  # Sudah dari CL, tidak perlu update

        self.beat_price       = price
        self.beat_source      = "CHAINLINK"
        self.beat_set_elapsed = self.elapsed
        self.beat_set_at      = time.time()
        return True

    def set_beat_from_hyperliquid(self, price: float) -> bool:
        """
        Set beat dari Hyperliquid — fallback jika Chainlink tidak tersedia.
        Tidak akan override jika sudah ada dari Chainlink.
        """
        if self.beat_source == "CHAINLINK":
            return False  # Pertahankan beat Chainlink
        return self.set_beat_price(price, source="HYPERLIQUID")

    @property
    def is_beat_reliable(self) -> bool:
        """
        True jika beat price kemungkinan akurat (sama dengan Polymarket).

        Reliable jika:
          - Source = CHAINLINK, atau
          - Source = HYPERLIQUID tapi di-set dalam 10 detik pertama window
        """
        if self.beat_price is None:
            return False
        if self.beat_source == "CHAINLINK":
            return True
        if self.beat_source == "HYPERLIQUID" and self.beat_set_elapsed <= 10:
            return True
        return False

    @property
    def beat_warning(self) -> str:
        """Peringatan tentang akurasi beat price, kosong jika aman."""
        if self.beat_price is None:
            return "Beat price belum tersedia"
        if self.beat_source == "CHAINLINK":
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
