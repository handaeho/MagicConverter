"""Explicit stage-7 capabilities. No speculative routes through lossy formats.

Filee's Data/Fonts/Ebooks/Email/Models3D/Geo converters informed this split;
the Python implementation and its narrower preservation contract are local.
"""

DATA = ("json", "jsonl", "yaml", "toml", "xml", "csv", "tsv")
FONTS = ("ttf", "otf", "woff", "woff2")
BOOKS = ("epub", "fb2")
MESHES = ("obj", "stl", "ply")
GEO = ("geojson", "kml", "kmz", "gpx")
FORMAT_SPECS = (
    ("json", "JSON", "data", ("json",)),
    ("jsonl", "JSON Lines", "data", ("jsonl", "ndjson")),
    ("yaml", "YAML", "data", ("yaml", "yml")),
    *((x, x.upper(), "data", (x,)) for x in ("toml", "xml")),
    *((x, x.upper(), "font", (x,)) for x in FONTS),
    ("fb2", "FictionBook", "ebook", ("fb2",)),
    ("cbz", "Comic ZIP", "ebook", ("cbz",)),
    ("eml", "EML", "email", ("eml",), True, False),
    ("mhtml", "MHTML", "email", ("mhtml", "mht"), True, False),
    *((x, x.upper(), "mesh", (x,)) for x in MESHES),
    *((x, x.upper(), "geo", (x,)) for x in GEO),
)


def routes():
    for family, formats in (
        ("data", DATA),
        ("font", FONTS),
        ("mesh", MESHES),
        ("geo", GEO),
    ):
        for source in formats:
            for target in formats:
                if source != target:
                    yield family, source, target, f"{family}:{target}"
    for source in (*BOOKS, "txt", "md", "docx"):
        for target in (*BOOKS, "html", "txt", "md", "docx"):
            if source not in BOOKS and target not in BOOKS:
                continue
            yield "ebook", source, target, (
                f"ebook:{target}" if target == "fb2" else f"document:{target}"
            )
    for target in ("pdf", "epub"):
        yield "ebook", "cbz", target, f"document:{target}"
    yield "ebook", "pdf", "cbz", "ebook:cbz"
    for source in ("eml", "mhtml"):
        for target in ("html", "txt"):
            yield "email", source, target, f"document:{target}"
    yield "email", "eml", "zip", "email:attachments"


def visible_keys(format_id):
    """Use exactly the same direct capabilities in the wheel and execution."""
    keys = tuple(
        dict.fromkeys(key for _, source, _, key in routes() if source == format_id)
    )
    if format_id in BOOKS:
        keys += ("document:pdf", "document:jpg", "document:png")
    return keys


def target_action(paths, target, catalog):
    from magic_converter.core.conversion.file_router import ALL_ACTIONS
    from magic_converter.core.conversion.formats import FORMATS

    detected = FORMATS.detect("file." + target.lower().lstrip("."))
    suffix = detected.suffix if detected else "." + target.lower().lstrip(".")
    return next(
        (
            key
            for key, action in ALL_ACTIONS.items()
            if action.suffix == suffix
            and all(catalog.assess(path, key).route is not None for path in paths)
        ),
        None,
    )
