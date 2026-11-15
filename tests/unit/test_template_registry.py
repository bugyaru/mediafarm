import uuid

import pytest

from mf_core.errors import ConfigInvalidError
from mf_core.storage.base import StorageRow
from mf_core.storage.registry import StorageRegistry, _norm, parse_uri
from mf_core.storage.template import build_context, render, sanitize


def test_render_tokens():
    ctx = build_context(asset_uri="smb://h/s/Show EP01.mxf", preset_name="proxy_1080p",
                        job_id="abc", asset_meta={"show": "News"})
    out = render("{yyyy}/{mm}/{asset.stem}_{preset.name}.mp4", ctx)
    assert out.startswith("20") and out.endswith("Show_EP01_proxy_1080p.mp4")
    assert render("{meta.show}/{job.id}.mkv", ctx) == "News/abc.mkv"


def test_unknown_token_and_traversal():
    ctx = build_context(asset_uri="a.mp4")
    with pytest.raises(ConfigInvalidError):
        render("{nope}/x.mp4", ctx)
    with pytest.raises(ConfigInvalidError):
        render("../{asset.name}", ctx)


def test_sanitize():
    assert sanitize("a/b:c*d?.mp4") == "a_b_c_d_.mp4"
    assert sanitize("...") == "file"


def test_norm_and_parse():
    assert _norm("s3://Bucket/prefix/") == "s3://bucket/prefix"
    assert _norm("/data/in/") == "file:///data/in"
    p = parse_uri("smb://fs01/share/dir%20one/f.mxf")
    assert p.host == "fs01" and p.path == "/share/dir one/f.mxf"


def test_longest_prefix_match():
    reg = StorageRegistry()
    a = StorageRow(uuid.uuid4(), "root", "s3", "s3://bkt")
    b = StorageRow(uuid.uuid4(), "deep", "s3", "s3://bkt/in/2026")
    reg._rows = {_norm(a.base_uri): a, _norm(b.base_uri): b}
    reg._by_name = {a.name: a, b.name: b}
    assert reg._match("s3://bkt/in/2026/f.mxf").name == "deep"
    assert reg._match("s3://bkt/other/f.mxf").name == "root"
    with pytest.raises(ConfigInvalidError):
        reg._match("s3://another/f.mxf")
        