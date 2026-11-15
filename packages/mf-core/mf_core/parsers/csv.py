from __future__ import annotations

import csv
import io

from mf_core.errors import ConfigInvalidError
from mf_core.parsers import ParseContext, SourceSpec, register, spec_from_row


@register("csv")
class CsvParser:
    id = "csv"

    async def parse(self, ctx: ParseContext) -> list[SourceSpec]:
        opts = ctx.options
        text = await ctx.read_text(opts.get("encoding"))
        delimiter = opts.get("delimiter", ",")
        try:
            if opts.get("sniff"):
                delimiter = csv.Sniffer().sniff(text[:4096]).delimiter
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter,
                                    quotechar=opts.get("quotechar", '"'))
            rows = list(reader)
        except csv.Error as e:
            raise ConfigInvalidError(f"{ctx.entry.uri}: битый CSV ({e})") from e

        limit = int(opts.get("limit") or 0)
        if limit:
            rows = rows[:limit]
        out: list[SourceSpec] = []
        for row in rows:
            clean = { (k or "").strip(): (v.strip() if isinstance(v, str) else v)
                      for k, v in row.items() if k }
            spec = spec_from_row(
                ctx, clean,
                url_field=opts.get("url_column", "url"),
                url_template=opts.get("url_template"),
                preset_field=opts.get("preset_column", "preset"),
                priority_field=opts.get("priority_column", "priority"),
                identity_field=opts.get("identity_column", "id"),
                meta_fields=opts.get("meta_columns"),
                default_kind=opts.get("kind"),
            )
            if spec is not None:
                out.append(spec)
        return out