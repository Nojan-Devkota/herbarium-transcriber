"""SAM 3.1 wire-format parsing, cost accounting and client selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.clients import get_client
from app.clients.meta_api import MetaModelClient, parse_sam_output
from app.clients.mock import MockClient


def test_parse_two_objects_with_confidence():
    text = (
        "<0f>0<|box;x1=587;y1=1181;x2=929;y2=1514;w=934;h=1600;c=0.68|><|mask;x=0;y=0;c=0.68;data=3,4,!abc|>,"
        "7<|box;x1=452;y1=1407;x2=565;y2=1474;w=934;h=1600;c=0.71|><|mask;x=0;y=0;data=2,2,!zz|>"
    )
    dets, w, h = parse_sam_output(text)
    assert (w, h) == (934, 1600)
    assert [(d.x1, d.y1, d.x2, d.y2) for d in dets] == [(587, 1181, 929, 1514), (452, 1407, 565, 1474)]
    assert dets[0].score == pytest.approx(0.68)


def test_parse_empty_is_zero_matches():
    assert parse_sam_output("") == ([], None, None)


def test_parse_without_confidence_and_skips_malformed():
    text = "<0f>0<|box;x1=1;y1=2;x2=3;y2=4;w=10;h=10|><|mask;data=1,1,~a|>,1<|box;x1=oops;w=10;h=10|>"
    dets, _, _ = parse_sam_output(text)
    assert len(dets) == 1 and dets[0].score is None


def test_parse_only_requested_frame():
    text = "<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=9;h=9|><1f>0<|box;x1=5;y1=5;x2=6;y2=6;w=9;h=9|>"
    assert [d.x1 for d in parse_sam_output(text, frame=1)[0]] == [5]


def test_spark_cost_uses_cached_and_output_prices(settings):
    settings.meta_api_key = "test-key"
    client = MetaModelClient(settings)
    resp = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=2_000_000,
            completion_tokens=1_000_000,
            prompt_tokens_details=SimpleNamespace(cached_tokens=1_000_000),
        )
    )
    usage = client._usage(resp, 0.0)
    # 1M uncached * 1.25 + 1M cached * 0.15 + 1M output * 4.25
    assert usage.cost_usd == pytest.approx(1.25 + 0.15 + 4.25)


def test_meta_client_requires_key(settings):
    settings.meta_api_key = ""
    with pytest.raises(RuntimeError, match="META_API_KEY"):
        MetaModelClient(settings)


def test_get_client_honours_mock_setting(settings):
    assert isinstance(get_client(settings), MockClient)
