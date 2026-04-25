"""
executor/polymarket.py
======================
Eksekutor order ke Polymarket via CLOB (Central Limit Order Book) API.

Revisi:
  - Implementasi Gamma API Slug Fetching berdasarkan dokumentasi Polymarket.
  - Generasi Timestamp akurat untuk membidik market 5-menit (interval 300 detik).

FIXES:
  - BUG #2: _place_gtc() sekarang pakai LimitOrderArgs object (bukan dict)
            agar tidak crash dengan "'dict' object has no attribute 'token_id'"
"""

import logging
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# Polymarket API endpoints
CLOB_BASE_URL  = "https://clob.polymarket.com"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DATA_BASE_URL  = "https://data-api.polymarket.com"

# USDC contract di Polygon (untuk on-chain fallback)
USDC_POLYGON   = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_DECIMALS  = 6

# Polygon RPC untuk fallback on-chain balance
POLYGON_RPCS = [
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://matic-mainnet.chainstacklabs.com",
]

# Tick size Polymarket
POLYMARKET_TICK     = Decimal("0.01")
POLYMARKET_TICK_STR = "0.01"


class PolymarketRelayer:
    """Handle gasless transactions via Polymarket Relayer."""

    RELAYER_URL = "https://relayer.polymarket.com"

    def __init__(self, api_key: str, api_key_address: str, private_key: str, funder: str):
        self._api_key = api_key
        self._api_key_address = api_key_address
        self._funder = funder
        self._account = None

        try:
            from eth_account import Account
            self._account = Account.from_key(private_key)
        except Exception as e:
            logger.error(f"[Relayer] Failed init account: {e}")

    def is_available(self) -> bool:
        return bool(self._api_key and self._account and self._funder)

    def _get_headers(self) -> dict:
        return {
            "POLY_ADDRESS":    self._api_key_address,
            "POLY_API_KEY":    self._api_key,
            "Content-Type":    "application/json",
        }

    def redeem_positions(self, condition_id: str) -> bool:
        if not self.is_available():
            return False
        try:
            payload = {"conditionId": condition_id, "funder": self._funder}
            resp = requests.post(
                f"{self.RELAYER_URL}/redeem",
                json=payload,
                headers=self._get_headers(),
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"[Relayer] Redeemed conditionId={condition_id[:16]}...")
                return True
            else:
                logger.warning(f"[Relayer] Redeem failed: {resp.status_code} {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"[Relayer] Redeem error: {e}")
            return False


class PolymarketExecutor:
    """Eksekutor utama untuk semua operasi Polymarket."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.balance: float = 0.0
        self._client = None
        self._relayer: Optional[PolymarketRelayer] = None
        self._initialized = False
        self._market_cache:    dict = {}
        self._market_cache_ts: dict = {}
        self._odds_cache: dict = {}
        self._odds_cache_time: float = 0.0

        self._has_partial_options = False
        self._has_create_order    = False

        self._private_key  = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        self._funder       = os.getenv("POLYMARKET_FUNDER", "")
        self._api_key      = os.getenv("POLYMARKET_API_KEY", "")
        self._api_secret   = os.getenv("POLYMARKET_API_SECRET", "")
        self._api_pass     = os.getenv("POLYMARKET_API_PASSPHRASE", "")
        self._relayer_key  = os.getenv("RELAYER_API_KEY", "")
        self._relayer_addr = os.getenv("RELAYER_API_KEY_ADDRESS", "")

        self._init()

    def _init(self) -> None:
        if not self._private_key:
            logger.warning("[Executor] POLYMARKET_PRIVATE_KEY tidak ditemukan di .env")
            return

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_pass,
            )

            self._client = ClobClient(
                host=CLOB_BASE_URL,
                key=self._private_key,
                chain_id=137,
                creds=creds,
                funder=self._funder,
                signature_type=1,
            )

            try:
                from py_clob_client.clob_types import PartialCreateOrderOptions
                self._has_partial_options = True
            except ImportError:
                self._has_partial_options = False

            self._has_create_order = callable(getattr(self._client, "create_order", None))

            if self._relayer_key and self._relayer_addr:
                self._relayer = PolymarketRelayer(
                    api_key=self._relayer_key,
                    api_key_address=self._relayer_addr,
                    private_key=self._private_key,
                    funder=self._funder,
                )

            self._initialized = True
            logger.info("[Executor] CLOB client initialized (sig_type=1 POLY_PROXY)")

        except ImportError:
            logger.error("[Executor] py-clob-client tidak terinstall.")
        except Exception as e:
            logger.error(f"[Executor] Init error: {e}")

    # ── VALIDATOR SAFETY ──────────────────────────────────────

    def _is_target_window(self, end_date_str: str) -> bool:
        if not end_date_str:
            return True
        try:
            clean_date = end_date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_date)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            remaining = dt.timestamp() - time.time()
            if remaining > 7200:
                return False
            return True
        except Exception:
            return True

    def _is_valid_5m_market(self, question: str, slug: str) -> bool:
        q = question.lower()
        s = slug.lower()
        has_5m = ("5 min" in q or "5-min" in q or "5m" in s)
        has_forbidden = ("15" in q or "15m" in s or "30" in q or "day" in q or "daily" in q)
        return has_5m and not has_forbidden

    # ── Balance ───────────────────────────────────────────────

    def get_balance(self) -> float:
        if self._initialized and self._client:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                resp   = self._client.get_balance_allowance(params)
                raw_balance = None
                if isinstance(resp, dict):
                    raw_balance = resp.get("balance")
                    if isinstance(raw_balance, dict):
                        raw_balance = (
                            raw_balance.get("decimal")
                            or raw_balance.get("value")
                            or raw_balance.get("balance")
                        )
                if raw_balance is not None:
                    raw_float = float(raw_balance)
                    self.balance = raw_float / 1_000_000 if raw_float > 1_000 else raw_float
                    return self.balance
            except Exception:
                pass
        if self._funder:
            try:
                from web3 import Web3
                USDC_ABI = [{"inputs": [{"name": "account", "type": "address"}],
                             "name": "balanceOf",
                             "outputs": [{"name": "", "type": "uint256"}],
                             "stateMutability": "view", "type": "function"}]
                for rpc in POLYGON_RPCS:
                    try:
                        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
                        if not w3.is_connected(): continue
                        contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_POLYGON), abi=USDC_ABI)
                        raw = contract.functions.balanceOf(Web3.to_checksum_address(self._funder)).call()
                        self.balance = raw / (10 ** USDC_DECIMALS)
                        return self.balance
                    except Exception:
                        continue
            except Exception:
                pass
        return self.balance

    # ── Market fetching ───────────────────────────────────────

    COIN_QUESTION_KW = {
        "BTC":  ["bitcoin", "btc"],
        "ETH":  ["ethereum", "eth"],
        "SOL":  ["solana", "sol"],
        "DOGE": ["dogecoin", "doge"],
        "XRP":  ["xrp", "ripple"],
    }

    def get_active_btc_market(self, force_refresh: bool = False) -> Optional[dict]:
        return self.get_active_market("BTC", force_refresh)

    def get_active_market(self, coin: str = "BTC", force_refresh: bool = False) -> Optional[dict]:
        coin = coin.upper()
        now  = time.time()

        if (not force_refresh
                and coin in self._market_cache
                and (now - self._market_cache_ts.get(coin, 0)) < 30):
            return self._market_cache[coin]

        result = (
            self._fetch_market_via_events(coin)
            or self._fetch_market_via_clob(coin)
            or self._fetch_market_via_search(coin)
        )

        if result:
            self._market_cache[coin]    = result
            self._market_cache_ts[coin] = now
            logger.info(f"[Executor] Market {coin} found: {result.get('question','')[:60]}")

        return result or self._market_cache.get(coin)

    def _fetch_market_via_events(self, coin: str) -> Optional[dict]:
        now = time.time()
        ts_current = int(now // 300) * 300
        slugs_to_try = [
            f"{coin.lower()}-updown-5m-{ts_current}",
            f"{coin.lower()}-updown-5m-{ts_current + 300}"
        ]
        try:
            for slug in slugs_to_try:
                resp = requests.get(
                    f"{GAMMA_BASE_URL}/events",
                    params={"slug": slug, "active": "true"},
                    timeout=5,
                )
                if resp.status_code != 200:
                    continue
                events = resp.json()
                if not isinstance(events, list) or len(events) == 0:
                    continue
                ev = events[0]
                markets = ev.get("markets", [])
                if not markets:
                    continue
                m = markets[0]
                if not m.get("acceptingOrders", False):
                    continue
                result = self._parse_market_dict(m, coin)
                if result and self._is_target_window(result.get("end_date", "")):
                    result["question"] = ev.get("title", result.get("question", ""))
                    return result
        except Exception as e:
            logger.debug(f"[Executor] _fetch_market_via_events({coin}) Error: {e}")
        return None

    def _fetch_market_via_clob(self, coin: str) -> Optional[dict]:
        kw_list = self.COIN_QUESTION_KW.get(coin, [coin.lower()])
        try:
            resp = requests.get(
                f"{CLOB_BASE_URL}/markets",
                params={"active": "true", "closed": "false", "limit": "100"},
                timeout=5,
            )
            if resp.status_code != 200: return None
            data    = resp.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            for m in markets:
                q    = m.get("question", "").lower()
                slug = m.get("market_slug", "").lower()
                coin_match = any(k in q or k in slug for k in kw_list)
                dir_match  = "up" in q and "down" in q
                if coin_match and dir_match and self._is_valid_5m_market(q, slug):
                    token_up = token_down = None
                    for t in m.get("tokens", []):
                        out = t.get("outcome", "").upper()
                        if out == "UP": token_up = t.get("token_id")
                        elif out == "DOWN": token_down = t.get("token_id")
                    if token_up and token_down:
                        end_date = m.get("end_date_iso", "")
                        if self._is_target_window(end_date):
                            return {
                                "coin":          coin,
                                "market_id":     m.get("condition_id") or m.get("conditionId"),
                                "question":      m.get("question", ""),
                                "token_id_up":   token_up,
                                "token_id_down": token_down,
                                "end_date":      end_date,
                            }
        except Exception as e:
            logger.debug(f"[Executor] _fetch_market_via_clob({coin}): {e}")
        return None

    def _fetch_market_via_search(self, coin: str) -> Optional[dict]:
        kw_list = self.COIN_QUESTION_KW.get(coin, [coin.lower()])
        try:
            for kw in kw_list[:1]:
                resp = requests.get(
                    f"{GAMMA_BASE_URL}/markets",
                    params={"search": f"{kw} up or down 5", "active": "true", "limit": "10"},
                    timeout=5,
                )
                if resp.status_code != 200: continue
                for m in resp.json():
                    question = m.get("question", "").lower()
                    slug     = m.get("slug", "").lower()
                    coin_ok  = any(k in question or k in slug for k in kw_list)
                    dir_ok   = "up" in question and "down" in question
                    if coin_ok and dir_ok and self._is_valid_5m_market(question, slug):
                        result = self._parse_market_dict(m, coin)
                        if result and self._is_target_window(result.get("end_date", "")):
                            return result
        except Exception as e:
            logger.debug(f"[Executor] _fetch_market_via_search({coin}): {e}")
        return None

    def _parse_market_dict(self, m: dict, coin: str) -> Optional[dict]:
        token_up = token_down = None
        def parse_field(val):
            if isinstance(val, list): return val
            if isinstance(val, str):
                try: import json; return json.loads(val)
                except Exception: return []
            return []

        clob_ids = parse_field(m.get("clobTokenIds", []))
        outcomes = parse_field(m.get("outcomes", []))

        if len(clob_ids) >= 2 and len(outcomes) >= 2:
            for i, out in enumerate(outcomes):
                out_str = str(out).strip().upper()
                if out_str in ("UP", "HIGHER", "YES") and i < len(clob_ids): token_up = clob_ids[i]
                elif out_str in ("DOWN", "LOWER", "NO") and i < len(clob_ids): token_down = clob_ids[i]
        elif len(clob_ids) >= 2:
            token_up, token_down = clob_ids[0], clob_ids[1]

        if not (token_up and token_down):
            for t in parse_field(m.get("tokens", [])):
                if not isinstance(t, dict): continue
                out = t.get("outcome", "").upper()
                if out in ("UP", "HIGHER", "YES"): token_up = t.get("token_id") or t.get("tokenId")
                elif out in ("DOWN", "LOWER", "NO"): token_down = t.get("token_id") or t.get("tokenId")

        if not (token_up and token_down): return None
        return {
            "coin":          coin,
            "market_id":     m.get("conditionId") or m.get("condition_id") or m.get("id"),
            "question":      m.get("question", f"{coin} Up or Down"),
            "token_id_up":   token_up,
            "token_id_down": token_down,
            "end_date":      m.get("endDate") or m.get("endDateIso") or m.get("end_date_iso", ""),
        }

    # ── Odds ──────────────────────────────────────────────────

    def get_odds(self, market: dict) -> tuple:
        now       = time.time()
        cache_key = market.get("market_id", "")
        if cache_key and (now - self._odds_cache_time) < 3:
            cached = self._odds_cache.get(cache_key)
            if cached: return cached

        try:
            cond_id = market.get("market_id", "")
            resp    = requests.get(f"{GAMMA_BASE_URL}/markets", params={"conditionId": cond_id}, timeout=4)
            if resp.status_code == 200:
                import json as _json
                data    = resp.json()
                markets = data if isinstance(data, list) else [data]
                target  = next((m for m in markets if m.get("conditionId") == cond_id), None)
                if not target and markets: target = markets[0]

                if target:
                    raw      = target.get("outcomePrices", "[]")
                    prices   = _json.loads(raw) if isinstance(raw, str) else raw
                    out_raw  = target.get("outcomes", "[]")
                    outcomes = _json.loads(out_raw) if isinstance(out_raw, str) else out_raw

                    if len(prices) >= 2:
                        odds_up = odds_down = 0.5
                        if len(outcomes) >= 2:
                            for i, out in enumerate(outcomes):
                                out_u = str(out).upper()
                                if out_u in ("UP", "HIGHER", "YES") and i < len(prices): odds_up = float(prices[i])
                                elif out_u in ("DOWN", "LOWER", "NO") and i < len(prices): odds_down = float(prices[i])
                        else:
                            odds_up, odds_down = float(prices[0]), float(prices[1])

                        if 0.01 < odds_up < 0.99 and 0.01 < odds_down < 0.99:
                            result = (odds_up, odds_down)
                            if cache_key:
                                self._odds_cache[cache_key] = result
                                self._odds_cache_time = now
                            return result
        except Exception: pass

        try:
            token_up, token_down = market["token_id_up"], market["token_id_down"]
            r1 = requests.get(f"{CLOB_BASE_URL}/midpoints", params={"token_ids": f"{token_up},{token_down}"}, timeout=3)
            if r1.status_code == 200:
                mids = r1.json()
                up, down = float(mids.get(token_up, 0) or 0), float(mids.get(token_down, 0) or 0)
                if 0.01 < up < 0.99 and 0.01 < down < 0.99:
                    result = (up, down)
                    if cache_key:
                        self._odds_cache[cache_key] = result
                        self._odds_cache_time = now
                    return result
        except Exception: pass

        return (0.5, 0.5)

    # ── Order placement ───────────────────────────────────────

    def _get_best_ask_live(self, token_id: str) -> Optional[float]:
        try:
            r = requests.get(
                f"{CLOB_BASE_URL}/book",
                params={"token_id": token_id},
                timeout=3,
            )
            if r.status_code != 200:
                return None
            book = r.json()
            asks = book.get("asks", [])
            if not asks:
                return None
            best = float(asks[0].get("price", 0))
            if 0.01 < best < 0.99:
                return best
        except Exception as e:
            logger.debug(f"[Executor] _get_best_ask_live error: {e}")
        return None

    def _submit_fok(self, token_id: str, amount_dec: float, price_dec: float,
                    side: str, direction: str) -> bool:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions

        market_args = MarketOrderArgs(
            token_id=token_id,
            price=price_dec,
            amount=amount_dec,
            side=side,
        )
        options = PartialCreateOrderOptions(tick_size=POLYMARKET_TICK_STR, neg_risk=False)
        signed  = self._client.create_market_order(market_args, options)
        if signed is None:
            logger.error("[Executor] create_market_order returned None")
            return False

        order_dict = signed.dict() if callable(getattr(signed, "dict", None)) else {}
        logger.info(
            f"[Executor] Submitting FOK {direction} @ {price_dec:.2f} | "
            f"maker={order_dict.get('makerAmount')} taker={order_dict.get('takerAmount')}"
        )

        resp = self._client.post_order(signed, OrderType.FOK)
        if resp and resp.get("status") in ("matched", "filled", "MATCHED"):
            logger.info(f"[Executor] ✓ FILLED {direction} ${amount_dec:.2f} @ {price_dec:.2f}")
            return True

        status = resp.get("status", "unknown") if resp else "no_response"
        err_msg = resp.get("error", "") if resp else ""
        is_fok_kill = (
            "fok" in err_msg.lower()
            or "fully filled" in err_msg.lower()
            or status in ("killed", "KILLED", "unmatched")
        )
        if is_fok_kill:
            logger.warning(f"[Executor] FOK killed @ {price_dec:.2f} (no liquidity at this price)")
            return False

        logger.warning(f"[Executor] Order not filled: status={status} err={err_msg[:100]}")
        return False

    def place_order(self, token_id: str, amount: float, side: str, price: float, direction: str) -> bool:
        """
        Submit FOK order dengan smart retry:
        1. Ambil best ask live dari order book
        2. Gunakan max(input_price, best_ask) sebagai starting price
        3. Jika FOK killed, naikkan price 1 tick dan retry (max 3x)
        4. Jika semua FOK gagal, fallback ke GTC (Good Till Cancelled)
        """
        if self.dry_run:
            logger.info(f"[Executor] DRY_RUN: {direction} ${amount:.2f} @ {price:.4f}")
            return True

        if not self._initialized or not self._client:
            return False

        TICK_2    = Decimal("0.01")
        amount_d  = Decimal(str(amount)).quantize(TICK_2, rounding=ROUND_DOWN)
        amount_dec = float(amount_d)
        if amount_d <= 0:
            return False

        live_ask = self._get_best_ask_live(token_id)
        if live_ask:
            logger.info(f"[Executor] Live best ask: {live_ask:.2f} | signal price: {price:.4f}")
            effective_price = max(price, live_ask)
        else:
            effective_price = price
            logger.debug(f"[Executor] No live ask, using signal price: {price:.4f}")

        price_d   = Decimal(str(effective_price)).quantize(TICK_2, rounding=ROUND_HALF_UP)
        price_dec = float(price_d)

        if price_d <= 0 or price_d >= 1:
            logger.warning(f"[Executor] Price out of range: {price_d}")
            return False

        logger.info(f"[Executor] place_order {direction}: amount={amount_dec} @ price={price_dec}")

        MAX_FOK_RETRIES = 3
        MAX_PRICE       = 0.97

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions

            for attempt in range(MAX_FOK_RETRIES):
                current_price_d   = price_d + Decimal("0.01") * attempt
                current_price_dec = float(current_price_d)

                if current_price_dec > MAX_PRICE:
                    logger.warning(f"[Executor] Price {current_price_dec} > MAX {MAX_PRICE}, berhenti retry")
                    break

                if attempt > 0:
                    logger.info(f"[Executor] FOK retry {attempt}/{MAX_FOK_RETRIES-1} @ {current_price_dec:.2f}")

                try:
                    filled = self._submit_fok(token_id, amount_dec, current_price_dec, side, direction)
                    if filled:
                        return True
                except Exception as e:
                    err = str(e).lower()
                    if "fok" in err or "fully filled" in err or "couldn't be" in err:
                        logger.warning(
                            f"[Executor] FOK exception @ {current_price_dec:.2f}: {str(e)[:80]}"
                        )
                        continue
                    self._log_order_err(e, direction)
                    return False

            # Semua FOK gagal → fallback ke GTC
            logger.warning(f"[Executor] Semua FOK gagal, fallback ke GTC @ {price_dec:.2f}")
            return self._place_gtc(token_id, amount_dec, price_dec, side, direction)

        except ImportError as e:
            logger.error(f"[Executor] Import error: {e}")
            return False
        except Exception as e:
            self._log_order_err(e, direction)
            return False

    def _place_gtc(self, token_id: str, amount_dec: float, price_dec: float,
                   side: str, direction: str) -> bool:
        """
        Fallback: kirim sebagai GTC (Good Till Cancelled).

        BUG #2 FIX: create_order() expects LimitOrderArgs object, bukan dict biasa.
        Sebelumnya: self._client.create_order({"token_id": ..., "price": ..., ...}, options)
        → crash: 'dict' object has no attribute 'token_id'

        Sekarang: gunakan LimitOrderArgs object yang benar.
        """
        try:
            from py_clob_client.clob_types import (
                CreateOrderOptions,
                OrderType,
                LimitOrderArgs,
            )

            # Gunakan LimitOrderArgs object — bukan dict
            order_args = LimitOrderArgs(
                token_id=token_id,
                price=price_dec,
                size=amount_dec,
                side=side,
            )
            options = CreateOrderOptions(tick_size=POLYMARKET_TICK_STR, neg_risk=False)
            signed  = self._client.create_order(order_args, options)

            if signed is None:
                logger.error("[Executor] GTC create_order returned None")
                return False

            resp = self._client.post_order(signed, OrderType.GTC)
            if resp and resp.get("status") in ("live", "matched", "LIVE", "MATCHED"):
                order_id = resp.get("orderID", resp.get("id", "?"))
                logger.info(f"[Executor] ✓ GTC order LIVE {direction} @ {price_dec:.2f} id={order_id[:12]}...")
                return True

            logger.warning(f"[Executor] GTC not accepted: {resp}")
            return False

        except ImportError:
            # LimitOrderArgs tidak tersedia di versi library ini — coba fallback lama
            logger.warning("[Executor] LimitOrderArgs tidak tersedia, coba fallback dict GTC")
            return self._place_gtc_legacy(token_id, amount_dec, price_dec, side, direction)
        except Exception as e:
            logger.error(f"[Executor] GTC fallback error: {e}")
            return False

    def _place_gtc_legacy(self, token_id: str, amount_dec: float, price_dec: float,
                          side: str, direction: str) -> bool:
        """
        Legacy GTC fallback jika LimitOrderArgs tidak tersedia di versi library lama.
        Ini adalah versi sebelum fix — hanya dipakai jika import LimitOrderArgs gagal.
        """
        try:
            from py_clob_client.clob_types import CreateOrderOptions, OrderType
            options = CreateOrderOptions(tick_size=POLYMARKET_TICK_STR, neg_risk=False)
            signed  = self._client.create_order(
                {
                    "token_id": token_id,
                    "price":    price_dec,
                    "size":     amount_dec,
                    "side":     side,
                },
                options,
            )
            if signed is None:
                return False
            resp = self._client.post_order(signed, OrderType.GTC)
            if resp and resp.get("status") in ("live", "matched", "LIVE", "MATCHED"):
                logger.info(f"[Executor] ✓ GTC legacy LIVE {direction} @ {price_dec:.2f}")
                return True
            return False
        except Exception as e:
            logger.error(f"[Executor] GTC legacy error: {e}")
            return False

    def _eval_resp(self, resp, direction: str, amount: float, price_dec: float) -> bool:
        if resp and resp.get("status") in ("matched", "filled", "MATCHED"):
            logger.info(f"[Executor] Order FILLED: {direction} ${amount:.2f} @ {price_dec:.4f}")
            return True
        logger.warning(f"[Executor] Order NOT filled: {direction}")
        return False

    def _log_order_err(self, e: Exception, direction: str) -> None:
        err = str(e).lower()
        if "no match" in err:
            logger.warning(f"[Executor] No counterparty untuk {direction}")
        elif "401" in err or "unauthorized" in err:
            logger.error(f"[Executor] Auth error: {e}")
        elif "tick_size" in err:
            logger.error(f"[Executor] tick_size error: {e} → coba: pip install py-clob-client --upgrade")
        else:
            logger.error(f"[Executor] Order error [{direction}]: {type(e).__name__}: {e}")

    # ── Positions & claim ─────────────────────────────────────

    def get_redeemable_positions(self) -> list:
        if not self._initialized or not self._funder: return []
        try:
            resp = requests.get(
                f"{DATA_BASE_URL}/positions",
                params={"user": self._funder, "redeemable": "true", "sizeThreshold": "0.01"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("positions", [])
        except Exception:
            return []

    def claim_position(self, condition_id: str) -> bool:
        if not self._relayer: return False
        return self._relayer.redeem_positions(condition_id)
