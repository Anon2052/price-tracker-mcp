"""Tests for affiliate link building."""

from __future__ import annotations

import pytest

from price_tracker.affiliate import AffiliateError, make_affiliate_link


def test_amazon_tag_added():
    url = make_affiliate_link("https://www.amazon.com/dp/B0EXAMPLE", "mytag-20")
    assert url == "https://www.amazon.com/dp/B0EXAMPLE?tag=mytag-20"


def test_amazon_tag_replaces_existing():
    url = make_affiliate_link("https://www.amazon.com/dp/B0X?tag=old-20&x=1", "new-20")
    assert "tag=new-20" in url
    assert "tag=old-20" not in url
    assert "x=1" in url


def test_custom_program_param():
    url = make_affiliate_link("https://shop.example.com/item/1", "aff123", program="ref")
    assert url == "https://shop.example.com/item/1?ref=aff123"


def test_invalid_tag_rejected():
    with pytest.raises(AffiliateError, match="tag"):
        make_affiliate_link("https://www.amazon.com/dp/B0X", "bad tag!")


def test_non_http_url_rejected():
    with pytest.raises(AffiliateError, match="http"):
        make_affiliate_link("ftp://example.com/x", "tag-20")


def test_unknown_program_rejected():
    with pytest.raises(AffiliateError, match="program"):
        make_affiliate_link("https://example.com/", "tag-20", program="not a param!")
