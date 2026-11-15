import uuid

import pytest

from mf_core.errors import ConfigInvalidError
from mf_core.parsers import ParseContext, get
from mf_core.storage.base import StorageRow
from mf_core.storage.local import LocalBackend

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.42c01e",NAME="low"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.640028",NAME="high"
high/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXTINF:6.0,
seg0.ts
#EXTINF:4.5,
seg1.ts
#EXT-X-ENDLIST
"""

ENCRYPTED = MEDIA.replace("#EXT-X-TARGETDURATION:6",
                          '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXT-X-TARGETDURATION:6')

MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT1M30.5S">
  <Period id="p1">
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="v1" bandwidth="3000000" width="1920" height="1080" codecs="avc1"/>
      <Representation id="v2" bandwidth="800000" width="640" height="360" codecs="avc1"/>
    </AdaptationSet>
    <AdaptationSet contentType="audio" lang="rus">
      <Representation id="a1" bandwidth="128000" codecs="mp4a.40.2"/>
    </AdaptationSet>
  </Period>
</MPD>
"""

XML = """<Batch xmlns:n="urn:mf">
  <Clip id="c1"><n:Title>News 1</n:Title><File>a.mxf</File><Preset>imx50_pal_ffmbc</Preset></Clip>
  <Clip id="c2"><n:Title>News 2</n:Title><File>b.mxf</File><Priority>200</Priority></Clip>
</Batch>
"""


def make_ctx(tmp_path, name: str, content: str, options=None) -> ParseContext:
    (tmp_path / name).write_text(content, encoding="utf-8")
    row = StorageRow(uuid.uuid4(), "t", "local", f"file://{tmp_path}")
    be = LocalBackend(row)
    import asyncio
    entry = asyncio.get_event_loop().run_until_complete(be.stat(name))
    return ParseContext(backend=be, entry=entry, options=options or {}, base_uri=row.base_uri)


async def ctx_for(tmp_path, name, content, options=None) -> ParseContext:
    (tmp_path / name).write_text(content, encoding="utf-8")
    be = LocalBackend(StorageRow(uuid.uuid4(), "t", "local", f"file://{tmp_path}"))
    return ParseContext(backend=be, entry=await be.stat(name), options=options or {},
                        base_uri=be.row.base_uri)


async def test_m3u8_master_variants(tmp_path):
    ctx = await ctx_for(tmp_path, "master.m3u8", MASTER)
    specs = await get("m3u8").parse(ctx)
    assert len(specs) == 2
    assert specs[1].meta["height"] == 1080 and specs[1].meta["bandwidth"] == 6000000
    assert specs[0].uri.endswith("/low/index.m3u8")
    assert specs[0].identity.endswith("#low")


async def test_m3u8_master_best_only_and_filter(tmp_path):
    ctx = await ctx_for(tmp_path, "m.m3u8", MASTER, {"best_only": True})
    assert len(await get("m3u8").parse(ctx)) == 1
    ctx = await ctx_for(tmp_path, "m.m3u8", MASTER, {"max_height": 400})
    specs = await get("m3u8").parse(ctx)
    assert len(specs) == 1 and specs[0].meta["height"] == 360


async def test_m3u8_media_playlist_and_segments(tmp_path):
    ctx = await ctx_for(tmp_path, "p.m3u8", MEDIA)
    specs = await get("m3u8").parse(ctx)
    assert len(specs) == 1 and specs[0].meta["duration_s"] == 10.5
    assert specs[0].meta["segments"] == 2 and specs[0].meta["live"] is False
    ctx = await ctx_for(tmp_path, "p.m3u8", MEDIA, {"segments": True})
    segs = await get("m3u8").parse(ctx)
    assert len(segs) == 2 and segs[1].meta["segment_duration_s"] == 4.5


async def test_m3u8_encrypted_skipped(tmp_path):
    ctx = await ctx_for(tmp_path, "e.m3u8", ENCRYPTED)
    assert await get("m3u8").parse(ctx) == []
    ctx = await ctx_for(tmp_path, "e.m3u8", ENCRYPTED, {"skip_encrypted": False})
    assert (await get("m3u8").parse(ctx))[0].meta["encrypted"] is True


async def test_mpd_manifest_and_representations(tmp_path):
    ctx = await ctx_for(tmp_path, "m.mpd", MPD)
    specs = await get("mpd").parse(ctx)
    assert len(specs) == 1 and specs[0].meta["duration_s"] == 90.5
    assert len(specs[0].meta["representations"]) == 3
    assert specs[0].meta["languages"] == ["rus"]
    ctx = await ctx_for(tmp_path, "m.mpd", MPD,
                        {"mode": "representations", "min_bandwidth": 1000000})
    reps = await get("mpd").parse(ctx)
    assert len(reps) == 1 and reps[0].identity.endswith("p1:v1")


async def test_xml_mapping_with_namespace_and_attrs(tmp_path):
    opts = {
        "item_xpath": ".//Clip",
        "namespaces": {"n": "urn:mf"},
        "fields": {"id": "@id", "title": "n:Title", "url": "File",
                   "preset": "Preset", "priority": "Priority"},
        "url_field": "url", "identity_field": "id",
    }
    ctx = await ctx_for(tmp_path, "batch.xml", XML, opts)
    specs = await get("xml").parse(ctx)
    assert len(specs) == 2
    assert specs[0].meta["title"] == "News 1" and specs[0].preset_name == "imx50_pal_ffmbc"
    assert specs[1].priority == 200 and specs[1].uri.endswith("/b.mxf")


async def test_xml_url_template(tmp_path):
    opts = {"item_xpath": ".//Clip", "fields": {"clip": "@id"},
            "url_template": "{clip}.mxf", "identity_field": "clip"}
    ctx = await ctx_for(tmp_path, "b.xml", XML, opts)
    specs = await get("xml").parse(ctx)
    assert [s.uri.rsplit("/", 1)[-1] for s in specs] == ["c1.mxf", "c2.mxf"]


async def test_csv_and_json(tmp_path):
    csv_text = "id,url,preset,priority,show\nc1,http://h/a.mp4,proxy_1080p_cpu,150,News\n"
    ctx = await ctx_for(tmp_path, "m.csv", csv_text,
                        {"url_column": "url", "preset_column": "preset",
                         "priority_column": "priority", "identity_column": "id"})
    specs = await get("csv").parse(ctx)
    assert specs[0].kind == "url" and specs[0].priority == 150 and specs[0].meta["show"] == "News"

    json_text = '{"data":{"items":[{"id":"x","file":"a.mp4","title":"T"}]}}'
    ctx = await ctx_for(tmp_path, "m.json", json_text,
                        {"items_path": "data.items", "url_field": "file",
                         "identity_field": "id"})
    specs = await get("json").parse(ctx)
    assert specs[0].uri.endswith("/a.mp4") and specs[0].meta["title"] == "T"


async def test_bad_manifests_raise(tmp_path):
    ctx = await ctx_for(tmp_path, "bad.m3u8", "not a playlist")
    with pytest.raises(ConfigInvalidError):
        await get("m3u8").parse(ctx)
    ctx = await ctx_for(tmp_path, "bad.xml", "<a>", {"item_xpath": ".//x"})
    with pytest.raises(ConfigInvalidError):
        await get("xml").parse(ctx)
    with pytest.raises(ConfigInvalidError):
        get("nope")