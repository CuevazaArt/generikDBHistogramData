"""Regresion del resolver Alpha: rutina obligatoria del tester.

Cada regla arbitraria detectada en produccion debe tener un test aqui.
Mocks: BinanceDownloader.get_alpha_token_list y alpha_symbols_for_alpha_id
para no depender de la red.
"""
from unittest.mock import patch

import pytest

from binance_hist_downloader import BinanceDownloader


def _make_token(symbol, alpha_id, *, chain="Base", offline=False, offsell=False,
                liquidity="1000000", volume="500000", listing_ms=1_777_000_000_000):
    return {
        "symbol": symbol, "alphaId": alpha_id, "chainName": chain,
        "offline": offline, "offsell": offsell,
        "liquidity": liquidity, "volume24h": volume,
        "listingTime": listing_ms,
    }


def test_resolver_single_match_usdt_tradeable():
    dl = BinanceDownloader()
    with patch.object(dl, "get_alpha_token_list", return_value=[
        _make_token("FOO", "ALPHA_100"),
    ]), patch.object(dl, "alpha_symbols_for_alpha_id", return_value=["ALPHA_100USDT"]):
        assert dl.resolve_alpha_symbol("FOOUSDT") == "ALPHA_100USDT"


def test_resolver_falls_back_to_usdc_when_usdt_not_tradeable():
    """PHAROS-style: solo USDC tradeable."""
    dl = BinanceDownloader()
    with patch.object(dl, "get_alpha_token_list", return_value=[
        _make_token("PHAROS", "ALPHA_964", chain="Base"),
    ]), patch.object(dl, "alpha_symbols_for_alpha_id", return_value=["ALPHA_964USDC"]):
        # Pide USDT pero solo hay USDC -> fallback warning + retorna USDC
        out = dl.resolve_alpha_symbol("PHAROSUSDT")
        assert out == "ALPHA_964USDC"


def test_resolver_dedupes_offline_alphaids_preferring_active():
    """PLAY-style: 2 alphaIds, uno offline."""
    dl = BinanceDownloader()
    active = _make_token("PLAY", "ALPHA_822", chain="Base", offline=False, offsell=False, liquidity="831000")
    dead = _make_token("PLAY", "ALPHA_300", chain="BSC", offline=True, offsell=True, liquidity="27000")
    # El offline va PRIMERO en la lista para asegurar que el resolver no toma el primero ciegamente
    with patch.object(dl, "get_alpha_token_list", return_value=[dead, active]), \
         patch.object(dl, "alpha_symbols_for_alpha_id", return_value=["ALPHA_822USDT"]):
        out = dl.resolve_alpha_symbol("PLAYUSDT")
        assert out == "ALPHA_822USDT"  # el activo, no el offline


def test_resolver_prefers_higher_liquidity_when_both_active():
    """Tie-breaker entre dos candidatos activos: el de mayor liquidez gana."""
    dl = BinanceDownloader()
    low_liq = _make_token("BAR", "ALPHA_1", liquidity="100", volume="50")
    high_liq = _make_token("BAR", "ALPHA_2", liquidity="5000000", volume="100000")
    with patch.object(dl, "get_alpha_token_list", return_value=[low_liq, high_liq]), \
         patch.object(dl, "alpha_symbols_for_alpha_id", return_value=["ALPHA_2USDT"]):
        out = dl.resolve_alpha_symbol("BARUSDT")
        assert out == "ALPHA_2USDT"


def test_resolver_raises_when_symbol_unknown():
    dl = BinanceDownloader()
    with patch.object(dl, "get_alpha_token_list", return_value=[
        _make_token("BTC", "ALPHA_1"),
    ]):
        with pytest.raises(ValueError, match="not found"):
            dl.resolve_alpha_symbol("DOESNOTEXISTUSDT")


def test_resolver_passes_through_alpha_prefix():
    """Si ya viene como ALPHA_XXX, no se re-resolves."""
    dl = BinanceDownloader()
    assert dl.resolve_alpha_symbol("ALPHA_953USDT") == "ALPHA_953USDT"


def test_resolver_rejects_unknown_quote():
    dl = BinanceDownloader()
    with pytest.raises(ValueError, match="unsupported"):
        dl.resolve_alpha_symbol("FOOPESO")


def test_alpha_fatal_codes_include_invalid_symbol():
    """-1121 debe estar en ALPHA_FATAL_CODES para fallar rapido sin retry."""
    assert "-1121" in BinanceDownloader.ALPHA_FATAL_CODES


def test_alpha_end_of_stream_codes_include_no_records():
    """-1000 debe estar en sentinels para no romper la paginacion."""
    assert "-1000" in BinanceDownloader.ALPHA_END_OF_STREAM_CODES
